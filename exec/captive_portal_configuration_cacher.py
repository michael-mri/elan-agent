#!/usr/bin/env python3

from elan.neuron import Dendrite, Synapse
from elan.captive_portal import CONF_PATH, GUEST_ACCESS_CONF_PATH, Administrator

class ConfigurationCacher():
    def __init__(self):
        self.synapse = Synapse()

    def cp_conf_updated(self, data, path):
        for profile in data:
            self.synapse.hset(CONF_PATH, profile['id'], profile)
            
    def ga_conf_updated(self, data, path):
            for profile in data:
                self.synapse.hset(GUEST_ACCESS_CONF_PATH, profile['id'], profile)
                
    def admins_conf_updated(self, data, path):
            Administrator.delete_all()
            for profile in data:
                Administrator.add(**profile)
            
cacher = ConfigurationCacher()
dendrite = Dendrite()
#dendrite.subscribe_conf('captive-portal', cb=cacher.cp_conf_updated)
dendrite.subscribe_conf('guest-access',   cb=cacher.ga_conf_updated)
dendrite.subscribe_conf('administrator',  cb=cacher.admins_conf_updated)

dendrite.wait_complete()
