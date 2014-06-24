import pyrad.packet
from pyrad.client import Client
from pyrad.dictionary import Dictionary
import time, subprocess, re
from origin.neuron import Dentrite
from mako.template import Template

def pwd_authenticate(authenticator_id, user, pwd):
    srv = Client(server="127.0.0.1", authport=18122, secret="a2e4t6u8qmlskdvcbxnw",
                 dict=Dictionary("/origin/captive-portal/radius/dictionary"))
    
    req = srv.CreateAuthPacket(code=pyrad.packet.AccessRequest,
              User_Name=user, Connect_Info='authenticator={}'.format(authenticator_id) )
    req["User-Password"]=req.PwCrypt(pwd)
    
    reply = srv.SendPacket(req)
    
    return reply.code == pyrad.packet.AccessAccept


class AuthenticationProvider(Dentrite):
    
    def __init__(self):
        super().__init__('authentication')

        self.agent_id = None
        self.providers = [] # list of providers
        self.provider_confs = {} # providers configuration by id
        self.groups = [] # list of authentication groups 
        self.group_authentications = {}

        self.retrieve('agent/self')
        self.subscribe('authentication/provider')
        self.subscribe('authentication/group')


    def answer_cb(self, path, conf):
        conf_changed = False
        # update local configuration cache
        if path == 'agent/self':
            if self.agent_id != conf['id']:
                self.agent_id = conf['id']
                conf_changed = True
        elif path == 'authentication/provider':
            new_providers = conf
            if self.providers != new_providers:
                conf_changed = True
                self.providers = new_providers
                for provider in new_providers:
                    # subscribe to new providers conf
                    if provider['id'] not in self.provider_confs:
                        self.subscribe( 'authentication/provider/{}'.format(provider['id']) )

        elif path == 'authentication/group':
            new_groups = conf
            if self.groups != new_groups:
                conf_changed = True
                self.groups = new_groups
                for group in new_groups:
                    # subscribe to new providers conf
                    if group['id'] not in self.group_authentications:
                        self.subscribe('authentication/group/{}/authentications'.format(group['id']))
        elif re.match('authentication/provider/', path):
            if conf['id'] not in self.provider_confs or self.provider_confs[ conf['id'] ] != conf:
                self.provider_confs[ conf['id'] ] = conf
                conf_changed = True
        else: 
            m = re.match('authentication/group/(\d+)/authentications', path)
            if m:
                if int(m.group(1)) not in self.group_authentications or self.group_authentications[ int(m.group(1)) ] != conf:
                    self.group_authentications[ int(m.group(1)) ] = conf
                    conf_changed = True

        if self.agent_id and conf_changed:
            # Grab templates
            policy_template = Template(filename="/origin/authentication/freeradius/policy")
            ldap_template = Template(filename="/origin/authentication/freeradius/ldap-module")
            cc_auth_template = Template('''
                update reply {
                    &Origin-Auth-Provider := ${id}
                }
                cc-auth {
                    fail = 1
                }
            ''')
            rest_conf = Template(filename="/origin/authentication/freeradius/rest-module")

            module_conf = rest_conf.render(agent_id=self.agent_id)
    
            inner_switch_server_conf = ""
            # Generate the files if we have all the information...
            new_provided_services = set()
            for provider in self.providers:
                if provider['id'] not in self.provider_confs:
                    # we do not have all the info to generate conf file, abort
                    return
                provider_conf = self.provider_confs[ provider['id'] ]
                if provider_conf['type'] == 'LDAP' and self.agent_id in provider_conf['agents']:
                    module_conf += "\n" + ldap_template.render(**provider_conf)
                    inner_switch_server_conf += '''
                        case {id} {{
                            update reply {{
                                &Origin-Auth-Provider := {id}
                            }}
                            ldap-auth-{id} {{
                                fail = 1
                            }}
                        }}
                    '''.format(**provider_conf)
                    # also notify that we provide this auth
                    new_provided_services.add( 'authentication/provider/{id}/authenticate'.format(id=provider['id']) )
            for group in self.groups:
                if group['id'] not in self.group_authentications:
                    # wait until we get the info
                    return
                first = True
                inner_case = ""
                for auth in self.group_authentications[ group['id'] ]:
                    if auth['authentication'] not in self.provider_confs:
                        return
                    provider_conf = self.provider_confs[ auth['authentication'] ]
                    if not first:
                        inner_case += "if( notfound || fail ) {\n"
                    if provider_conf['type'] == 'LDAP' and self.agent_id in provider_conf['agents']:
                        inner_case += '''
                        update reply {{
                            &Origin-Auth-Provider := ${id}
                        }}
                        ldap-auth-{id} {{
                            fail = 1
                        }}
                        '''.format(**provider_conf)
                    else:
                        inner_case += cc_auth_template.render(**provider_conf)
                    if not first:
                        inner_case += "\n}\n"
                    else :
                        first = False
                inner_switch_server_conf +=  '''
                        case {id} {{
                            {inner_case}
                        }}
                    '''.format(id=group['id'], inner_case=inner_case)
    
            with open ("/etc/freeradius/mods-enabled/authentications", "w") as module_file:
                module_file.write( module_conf )
            with open ("/etc/freeradius/policy.d/authentications", "w") as policy_file:
                policy_file.write( policy_template.render(inner_switch=inner_switch_server_conf) )
    
            # unprovide
            for service_path in self.get_provided_services() - new_provided_services:
                self.unprovide(service_path)
                
            # Reload nginx
            subprocess.call('/etc/init.d/freeradius restart', shell=True)
            
            # new provides
            for service_path in new_provided_services:
                self.provide(service_path)

    def call_cb(self, path, data):
        m = re.match(r'authentication/provider/(\d+)/authenticate', path)
        if m:
            # TODO: have a way to detect failure of provider (LDAP...) in FR ... via exception ? based on RADIUS Reply-Message ?
            try:
                return { 'success': pwd_authenticate(m.group(1), data['login'], data['password']) }
            except KeyError:
                return { 'success': False }
