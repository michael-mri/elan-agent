import pyrad.packet
from pyrad.client import Client
from pyrad.dictionary import Dictionary
import subprocess, re, socket
from origin.neuron import Synapse
from origin.utils import restart_service
from mako.template import Template



def pwd_authenticate(authenticator_id, login, password, source):
    srv = Client(server="127.0.0.1", authport=18122, secret=b'a2e4t6u8qmlskdvcbxnw',
                 dict=Dictionary("/origin/authentication/pyradius/dictionary"))
    
    req = srv.CreateAuthPacket(code=pyrad.packet.AccessRequest,
              User_Name=login, Connect_Info='authenticator={},source={},command=authenticate'.format(authenticator_id, source) )
    req["User-Password"]=req.PwCrypt(password)
    
    reply = srv.SendPacket(req)
    
    return reply.code == pyrad.packet.AccessAccept

def get_authorization(authenticator_id, login, source):
    srv = Client(server="127.0.0.1", authport=18122, secret=b'a2e4t6u8qmlskdvcbxnw',
                 dict=Dictionary("/origin/authentication/pyradius/dictionary"))
    
    req = srv.CreateAuthPacket(code=pyrad.packet.AccessRequest,
              User_Name=login, Connect_Info='authenticator={},source={},command=authorize'.format(authenticator_id, source) )
    
    reply = srv.SendPacket(req)
    
    authz = {}
    
    for attr in reply.get(18, []): # 18 -> Reply-Message
        key, value = attr.split('=', 1)
        authz[key] = value
        
    return authz

class AuthenticationProvider():
    
    def __init__(self, dendrite=None):
        if dendrite is None:
            dendrite = dendrite()
        self.dendrite = dendrite
        
        self.agent_id = None
        self.authentications = {} # indexed by id

        self.policy_template = Template(filename="/origin/authentication/freeradius/policy")
        self.ldap_template = Template(filename="/origin/authentication/freeradius/ldap-module")
        self.ad_template = Template(filename="/origin/authentication/freeradius/ad-module")
        self.cc_auth_template = Template('''
            update session-state {
                &Origin-Auth-Provider := ${id}
            }
            external-auth {
                invalid = 1
                fail =  2
                reject = 3
                notfound = 4
                ok = return
                updated = return
            }
        ''')
        self.ldap_auth_template = Template('''
            update session-state {
                &Origin-Auth-Provider := ${id}
            }
            ldap-auth-${id} {
                invalid = 1
                fail =  2
                reject = 3
                notfound = 4
                ok = return
                updated = return
            }
        ''')
        self.ad_auth_template = Template('''
            update session-state {
                &Origin-Auth-Provider := ${id}
                &Origin-AD-Auth-Provider := ${id}
            }
            ADldap {
                invalid = 1
                fail =  2
                reject = 3
                notfound = 4
                ok = return
                updated = return
            }
        ''')
        self.google_auth_template = Template('''
            update session-state {
                &Origin-Auth-Provider := ${id}
            }
            external-auth.authenticate {
                invalid = 1
                fail =  2
                reject = 3
                notfound = 4
                ok = return
                updated = return
            }
        ''')

    def get_group_inner_case(self, auth, ignore_authentications=None):
        if ignore_authentications is None:
            ignore_authentications = set()
        
        if auth['id'] in ignore_authentications:
            # authentications has already been tried. no need to try it again...
            return ''

        ignore_authentications.add(auth['id'])

        inner_case = ''
        
        if auth['type'] == 'group':
            for member in auth['members']:
                inner_case += self.get_group_inner_case( self.authentications[member['authentication']], ignore_authentications )
        else:
            if auth['type'] == 'LDAP' and self.agent_id in auth['agents']:
                inner_case += self.ldap_auth_template.render(**auth)
            elif auth['type'] == 'active-directory' and self.agent_id in auth['agents']:
                inner_case += self.ad_auth_template.render(**auth)
            elif auth['type'] == 'google-apps':
                inner_case += self.google_auth_template.render(**auth)
            else:
                inner_case += self.cc_auth_template.render(**auth)
    
            inner_case += '''
                    if(fail) {
                        update request {
                            Origin-Auth-Failed := &session-state:Origin-Auth-Provider
                        }
                        auth_provider_failed_in_group
                        update request {
                            Module-Failure-Message !* ANY
                        }
                    }
                    elsif(! invalid) {
                        update {
                            request:Origin-Non-Failed-Auth := "True"
                        }
                    }
            '''
        
        return inner_case

    def agent_conf(self, agent):
            if self.agent_id != agent.id:
                self.agent_id = agent.id
                self.apply_conf()
                
    def new_authentication_conf(self, conf):
            new_authentications = {}
            for auth in conf:
                new_authentications[auth['id']] = auth
            
            if new_authentications != self.authentications:
                self.authentications = new_authentications
                self.apply_conf()

    def apply_conf(self):
        if self.agent_id is not None: # we may receive agent id after conf
            module_conf = ""
    
            inner_switch_server_conf = ""
            # Generate the files if we have all the information...
            new_provided_services = set()
            
            has_active_directory = False
            
            for auth in self.authentications.values():
                if auth['type'] == 'LDAP' and self.agent_id in auth['agents']:
                    module_conf += "\n" + self.ldap_template.render(**auth)
                    inner_switch_server_conf +=  '''
                        case {id} {{
                    '''.format(id=auth['id'])
                    inner_switch_server_conf += self.ldap_auth_template.render(**auth)
                    inner_switch_server_conf += '''
                            if(fail) {
                                update request {
                                    Origin-Auth-Failed := &session-state:Origin-Auth-Provider
                                }
                                auth_provider_failed
                                update request {
                                    Module-Failure-Message !* ANY
                                }
                            }
                        }
                    '''
                    # also notify that we provide this auth
                    new_provided_services.add( 'authentication/provider/{id}/authenticate'.format(id=auth['id']) )
                    new_provided_services.add( 'authentication/provider/{id}/authorize'.format(id=auth['id']) )
                elif auth['type'] == 'active-directory' and str(self.agent_id) in auth['agent_statuses']:
                    # Join domain if not already done
                    if not AD.joined(auth['domain']):
                        if AD.joined():
                            AD.leave()
                        # try to join
                        try: 
                            AD.join(realm=auth['domain'], user=auth['adminLogin'], password=auth['adminPwd'])
                        except AD.Error as e:
                            self.post('authentication/provider/{id}/join-failed'.format(id=auth['id']), {'detail': e.message})
                    
                    if AD.joined(auth['domain']):
                        # if auth status for this agent is not joined, update it...
                        if auth['agent_statuses'][str(self.agent_id)]['status'] != 'joined':
                            self.post('authentication/provider/{id}/join-success'.format(id=auth['id']), {})
                        
                        has_active_directory = True
                        inner_switch_server_conf +=  '''
                            case {id} {{
                        '''.format(id=auth['id'])
                        inner_switch_server_conf += self.ad_auth_template.render(**auth)
                        inner_switch_server_conf += '''
                                if(fail) {
                                    update request {
                                        Origin-Auth-Failed := &session-state:Origin-Auth-Provider
                                    }
                                    auth_provider_failed
                                    update request {
                                        Module-Failure-Message !* ANY
                                    }
                                }
                            }
                        '''
                        # also notify that we provide this auth
                        new_provided_services.add( 'authentication/provider/{id}/authenticate'.format(id=auth['id']) )
                        new_provided_services.add( 'authentication/provider/{id}/authorize'.format(id=auth['id']) )
                elif auth['type'] == 'group':
                    # Take care of groups, that can be nested:
                    inner_switch_server_conf +=  '''
                            case {id} {{
                                group {{
                                    {inner_case}
                                    if( ! &Origin-Non-Failed-Auth) {{
                                        auth_all_providers_failed_in_group
                                    }}
                                }}
                            }}
                    '''.format(
                           id = auth['id'],
                           inner_case = self.get_group_inner_case(auth) )
                elif auth['type'] == 'google-apps':
                    inner_switch_server_conf +=  '''
                            case {id} {{
                    '''.format(id=auth['id'])
                    inner_switch_server_conf += self.google_auth_template.render(**auth)
                    inner_switch_server_conf +=  '}'
            
            # Always add AD module conf as it is used even if no AD declared
            ad_info = AD.info() or {}
            module_conf += "\n" + self.ad_template.render(has_active_directory=has_active_directory, **ad_info)

            # Quit AD domain if required
            if not has_active_directory and AD.joined():
                AD.leave()

            with open ("/etc/freeradius/mods-enabled/authentications", "w") as module_file:
                module_file.write( module_conf )
            with open ("/etc/freeradius/policy.d/authentications", "w") as policy_file:
                policy_file.write( self.policy_template.render(inner_switch=inner_switch_server_conf) )
            
            # CAs
            for provider in self.authentications.values():
                if provider.get('server_ca', None):
                    with open ("/etc/freeradius/certs/server_CA/auth-{id}.pem".format(id=provider['id']), "w") as server_ca_file:
                        server_ca_file.write(provider['server_ca'])
    
            # unprovide
            for service_path in self.get_provided_services() - new_provided_services:
                self.dendrite.unprovide(service_path)
                
            # Reload freeradius
            restart_service('freeradius')
            
            # new provides
            for service_path in new_provided_services:
                self.dendrite.provide(service_path, cb=self.on_call)

    def on_call(self, data, service):
        # TODO: make this async....
        m = re.match(r'authentication/provider/(\d+)/authenticate', service)
        if m:
            # TODO: have a way to detect failure of provider (LDAP...) in FR ... via exception ? based on RADIUS Reply-Message ?
            try:
                return { 'success': pwd_authenticate(m.group(1), login=data['login'], password=data['password'], source=data['source']) }
            except KeyError:
                return { 'success': False }

        m = re.match(r'authentication/provider/(\d+)/authorize', service)
        if m:
            # TODO: have a way to detect failure of provider (LDAP...) in FR ... via exception ? based on RADIUS Reply-Message ?
            try:
                return get_authorization(m.group(1), login=data['login'], source=data['source'])
            except KeyError:
                return { 'success': False }
        
            

class AD:    
    synapse = Synapse()
    
    REDIS_INFO_PATH = 'authentication:AD:info'
    
    @classmethod
    def _run(cls, args):
        '''
        run command args as in subprocess.run
        returns a subprocess.CompletedProcess
        raises AD.Error with message as concatenation of stdout and stderr if return code != 0
        '''
        process_result =  subprocess.run(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True
        )
        if process_result.returncode != 0:
            raise cls.Error(process_result.stdout + '\n' + process_result.stderr)
        
        return process_result
    
    @classmethod
    def joined(cls, realm=None):
        AD_info = cls.synapse.get(cls.REDIS_INFO_PATH)
        
        if AD_info:
            if realm:
                return realm.upper() == AD_info['realm'].upper()
            else:
                return True
        return False

    @classmethod
    def leave(cls):
        '''leave AD domain
        
            never fails
        '''
        try:
            cls._run(['net', '-P', 'ads', 'leave'])
        except cls.Error:
            pass

    @classmethod
    def join(cls, realm, user, password):
        cls._run(['net', 'conf', 'setparm', 'global', 'dedicated keytab file', '/etc/krb5.keytab'])
        cls._run(['net', 'conf', 'setparm', 'global', 'kerberos method', 'dedicated keytab'])
        cls._run(['net', 'ads', 'join', '-U', '{user}%{password}'.format(user=user, password=password), realm])
        restart_service('winbind')
        cls._run(['net', '-P', 'ads', 'keytab', 'create'])
        cls._run(['chown', 'freerad', '/etc/krb5.keytab'])
        cls._run(['usermod', '-a', '-G', 'winbindd_priv', 'freerad'])
        cls._run(['chgrp', 'winbindd_priv', '/var/lib/samba/winbindd_privileged'])
        
        f = open('/etc/freeradius/.k5identity', 'w')
        f.write('{hostname}$@{realm}'.format(hostname=socket.gethostname(), realm=realm).upper())
        f.close()

        process_result = cls._run(['net', '-P', 'ads', 'info'])

        info = {}
        for line in process_result.stdout.split('\n'):
            try:
                key, value = line.split(': ', 2)
            except ValueError:
                pass
            else:
                if key == 'LDAP server':
                    info['ldap_server_ip'] = value
                if key == 'LDAP server name':
                    info['ldap_server_name'] = value
                elif key == 'LDAP port':
                    info['ldap_port'] = value
                elif key == 'Bind Path':
                    info['ldap_base_dn'] = value
                elif key == 'Realm':
                    info['realm'] = value
        cls.synapse.set(cls.REDIS_INFO_PATH, info)

    @classmethod
    def info(cls):
        return cls.synapse.get(cls.REDIS_INFO_PATH)
    
    class Error(Exception):
        def __init__(self, message):
            self.message = message


