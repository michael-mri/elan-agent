from origin.neuron import Dendrite, Synapse
from origin.event import Event
from origin import nac, session

CONF_PATH = 'conf:captive-portal'
GUEST_ACCESS_CONF_PATH = 'conf:guest-access'

def submit_guest_request(request):
    ''' submits sponsored guest access request and return ID of request'''
    d = GuestAccessManager()
    r = d.sync_post('guest-request', request)
    request_id = r['id']
    
    d.synapse.sadd('guest-request:authz_pending:{vlan}'.format(vlan=r['vlan_id']),request['mac'])
    #d.synapse.rpush('guest-request:mac_request:'+request['mac'], request_id)
    
    # Subscribe to any changes
    # TODO subscribe to something like guest-request/active that would send only updates on active (granted) requests
    d.subscribe('guest-request/' + request_id)
    
    
    return request_id

def is_authz_pending(mac, vlan):
    return Synapse().sismember('guest-request:authz_pending:{vlan}'.format(vlan=vlan), mac)

class Administrator:
    ADMINISTRATOR_CONF_PATH = 'conf:administrator'
    synapse = Synapse()
    @classmethod
    def get(cls, login):
        params = cls.synapse.hget(cls.ADMINISTRATOR_CONF_PATH, login)
        if not params:
            return None
        return cls(login=login, **params)
    
    @classmethod
    def add(cls, **kwargs):
        if 'email' in kwargs and 'password' in kwargs:
            login = kwargs.pop('email')
            cls.synapse.hset(cls.ADMINISTRATOR_CONF_PATH, login, kwargs)
            return True
        return False

    @classmethod
    def delete_all(cls):
        cls.synapse.delete(cls.ADMINISTRATOR_CONF_PATH)


    def __init__(self, login, password, **kwargs):
        self.login = login
        self.password = password
        for key in kwargs:
            setattr(self, key, kwargs[key])
        
    def check_password(self, password):
        from django.contrib.auth.hashers import check_password
        return check_password(password, self.password)
        
class GuestAccessManager(Dendrite):
    def __init__(self):
        super().__init__('guest-access-manager')
    
    def answer_cb(self, path, answer):
        if answer['authorizations']:
            last_authz = answer['authorizations'][-1]
            mac = answer['mac']
            vlan_id = answer['vlan_id']
            
            # Authz not pending any more
            self.synapse.srem('guest-request:authz_pending:{vlan}'.format(vlan=vlan_id), mac)
            
            if not last_authz['end']:
                if last_authz['end_authorization']:
                    import dateutil.parser
                    till_date=dateutil.parser.parse(last_authz['end_authorization'])
                else:
                    till_date=None

                session.add_authentication_session(mac, source='guest', till_date=till_date, login=last_authz['login'], authentication_provider=last_authz['authentication_provider'])

                assignments = session.get_network_assignments(mac)
                if assignments:
                    Event( 'device-authorization', source='guest-access')\
                        .add_data('mac', mac, 'mac')\
                        .add_data('authentication_provider', last_authz['authentication_provider'], 'authentication_provider')\
                        .add_data('login', last_authz['login'])\
                        .add_data('authorized', assignments['bridge'] , 'bool')\
                        .add_data('vlan', assignments['vlan'])\
                        .notify()
                    
                    if assignments['vlan'] != vlan_id:
                        pass  # TODO: move to new vlan + async disconnect on this VLAN.-> do we have current vlan ?
                    if assignments['bridge']:
                        nac.allowMAC(mac, assignments['vlan'], till_disconnect=True)
                    #TODO: update authorisation with vlan ???? 

            elif last_authz['termination_reason'] == 'revoked':
                nac.disallowMAC(mac, vlan_id, reason='revoked')
            