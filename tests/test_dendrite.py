import unittest
import concurrent.futures
import threading
import json

from origin.neuron import Dendrite
from paho.mqtt import client, publish
import time

def run_in_thread(target, *args, **kwargs):
    fut = concurrent.futures.Future()
    def run():
        result = target(*args, **kwargs)
        fut.set_result( result )
    
    threading.Thread(target=run).start()
    
    return fut

class DendriteTest(unittest.TestCase):
    def setUp(self):
        self.dendrite = Dendrite()
        self.mqtt = client.Client()
        self.mqtt.connect(Dendrite.MQTT_HOST, Dendrite.MQTT_PORT)
    
    def tearDown(self):
        self.dendrite.finish()
        self.mqtt.disconnect()

    def test_publish(self):
        topic1 = 'test/topic1'
        topic2 = 'test/topic2'
        msg1 = dict(test='OK')
        
        
        self.mqtt.subscribe(topic1)
        self.mqtt.subscribe(topic2)
        
        future = concurrent.futures.Future()
        
        def on_message(client, userdata, message):
            future.set_result(message)
        self.mqtt.on_message = on_message
        self.mqtt.loop_start()
        
        self.dendrite.publish(topic1, msg1)

        msg = future.result(2)
            
        self.assertEqual(json.loads(msg.payload.decode()), msg1)
        self.assertEqual(msg.topic, topic1)
        
        msg2=dict(test=dict(OK='ok'), OK='ok')

        results = []
        future = concurrent.futures.Future()
        def on_message2(client, userdata, message):
            results.append(message)
            if len(results) == 3:
                future.set_result(True)
        self.mqtt.on_message = on_message2

        self.dendrite.publish(topic2, msg1)
        self.dendrite.publish(topic2, msg2)
        self.dendrite.publish(topic1, msg2)
        
        future.result(2)
        
        self.assertEqual(json.loads(results[0].payload.decode()), msg1)
        self.assertEqual(results[0].topic, topic2)

        self.assertEqual(json.loads(results[1].payload.decode()), msg2)
        self.assertEqual(results[1].topic, topic2)

        self.assertEqual(json.loads(results[2].payload.decode()), msg2)
        self.assertEqual(results[2].topic, topic1)
        

    def test_subscribe(self):
        topic1 = 'test/topic1'
        msg1 = dict(test='OK', msg=1)

        future = concurrent.futures.Future()
        def get_msg(msg, topic):
            future.set_result({ 'msg': msg, 'topic': topic})
        
        
        
        self.dendrite.subscribe(topic1, get_msg)
        
        time.sleep(1)
        
        self.mqtt.publish(topic1, json.dumps(msg1))
        
        result = future.result(2)
        self.assertEqual(result, {'msg': msg1, 'topic': topic1})

        
        
    def test_subscribe_1arg(self):
        topic1 = 'test/topic1'
        msg1 = dict(test='OK', msg=1)

        future = concurrent.futures.Future()
        def get_msg(msg):
            future.set_result(msg)
        
        self.dendrite.subscribe(topic1, get_msg)
        
        time.sleep(1)
        
        self.mqtt.publish(topic1, json.dumps(msg1))
        
        result = future.result(2)
        
        self.assertEqual(result, msg1)
        
    def test_subscribe_1arg_method(self):
        topic1 = 'test/topic1'
        msg1 = dict(test='OK', msg=1)

        future = concurrent.futures.Future()
        class Dummy:
            def get_msg(self, msg):
                future.set_result(msg)
        
        d = Dummy()
        self.dendrite.subscribe(topic1, d.get_msg)
        
        time.sleep(1)
        
        self.mqtt.publish(topic1, json.dumps(msg1))
        
        result = future.result(2)
        
        self.assertEqual(result, msg1)

    def test_provide(self):
        def cb(data):
            return 'Test OK: ' + data
        
        
        service = 'test/fct'
        
        self.dendrite.provide('test/fct', cb)
        
        result = self.dendrite.call_provided(service, 'sent')
        
        self.assertEqual(result, 'Test OK: sent')

    def test_call(self):
        
        def rpc(data, topic):
            service = topic[len(Dendrite.RPC_REQUESTS_PATH_PREFIX):]
            self.dendrite.publish(Dendrite.RPC_ANSWERS_PATH_PREFIX + data['request_id'], 'RPC {service}: got "{data}"'.format(service=service, **data))

        service = 'My Service'
        self.dendrite.subscribe(Dendrite.RPC_REQUESTS_PATH_PREFIX + service, rpc)

        result = self.dendrite.call(service, 'My Data', timeout=2)
                
        self.assertEqual(result, 'RPC {}: got "My Data"'.format(service))











        
        