from elan.neuron import Synapse
from elan.utils import restart_service, stop_service, start_service
from mako.template import Template


class NetworkConfiguration:
    IPv4_CONF_PATH = 'conf:network:ipv4'
    IPv6_CONF_PATH = 'conf:network:ipv6'
    configuration_template = '/elan-agent/network/interfaces' 
    configuration_file = '/etc/network/interfaces.d/ea-network' 
    synapse = Synapse()
    
    def __init__(self):
        self.load_configuration()
        
    def load_configuration(self):
        self.ipv4 = self.synapse.get(self.IPv4_CONF_PATH)
        if self.ipv4 is None:
            self.ipv4 = {'type': 'dhcp', 'dns': [] } # default conf
            
        self.ipv6 = self.synapse.get(self.IPv6_CONF_PATH)
        if self.ipv6 is None:
            self.ipv6 = {'type': 'autoconf', 'dns': [] } # default conf
            
    def save_configuration(self):
        self.synapse.set(self.IPv4_CONF_PATH, self.ipv4)
        self.synapse.set(self.IPv6_CONF_PATH, self.ipv6)
        
    def apply_configuration(self):
        stop_service('nac-network', sudo=True) # bring down br0 with old config to deconfigure it properly (DHCP release...)
        self.generate_configuration_files()
        start_service('nac-network', no_block=True, sudo=True)
    
    def generate_configuration_files(self):
        template = Template(filename=self.configuration_template)
        
        with open(self.configuration_file, 'w') as conf_file:
            conf_file.write( template.render(ipv4=self.ipv4, ipv6=self.ipv6) )
                
    
    def setIPv4Configuration(self, **kwargs):
        self.ipv4 = kwargs
        self.save_configuration()
        self.apply_configuration()

    def setIPv6Configuration(self, **kwargs):
        self.ipv6 = kwargs
        self.save_configuration()
        self.apply_configuration()
    
    @classmethod
    def reload(cls):
        restart_service('nac-network', no_block=True, sudo=True)
        