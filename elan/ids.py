from mako.template import Template
import netaddr

from elan import neuron, network

NETWORKS_CACHE_PATH = 'ids:cache:networks'


def generate_suricata_conf(force=False):
    ''' return True if conf has changed, False otherwise
     force to force generation of file when variables have not changed
    '''

    s = neuron.Synapse()
    netconf = network.NetworkConfiguration()

    networks = set()
    for ip in netconf.get_current_ips(cidr=True):
        if not ip.startswith('fe80'):
            ip_network = netaddr.IPNetwork(ip)
            networks.add('{}/{}'.format(ip_network.network, ip_network.prefixlen))

    if not networks:
        # check cached networks
        networks = s.smembers(NETWORKS_CACHE_PATH)

    if not networks:
        # default values:
        networks = set(['192.168.0.0/16', '10.0.0.0/8'])  # no defaults for IPv6 :(

    changed = False
    if s.smembers(NETWORKS_CACHE_PATH) != networks:
        changed = True

    if changed or force:
        # generate conf
        conf_template = Template(filename="/elan-agent/ids/suricata/conf")
        with open ("/etc/suricata/suricata.yaml", "w") as conf_file:
            conf_file.write(conf_template.render(networks=networks))

    if changed:
        # cache params
        s.pipe.delete(NETWORKS_CACHE_PATH)
        s.pipe.sadd(NETWORKS_CACHE_PATH, *networks)
        s.pipe.execute()

    return changed

