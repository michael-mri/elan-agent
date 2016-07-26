import json, datetime, traceback, time
import uuid
import redis
from .. import utils
import inspect
import concurrent.futures
import asyncio
import hbmqtt.client
from signal import SIGTERM

CACHE_PREFIX = 'cache:'

DENDRITE_COMMANDS = 'dendrite:commands'
DENDRITE_ANSWERS = 'dendrite:answers'
 
AXON_ANSWERS = 'axon:answers:{name}'
AXON_CALLS = 'axon:calls:{name}'

CALL_TIMEOUT = 60 # seconds
SYNC_CALL_TIMEOUT = 40 #seconds

def wait_for_synapse_ready():
    started = False
    s = Synapse()
    
    while not started:
        try:
            started = s.ping()
        except redis.exceptions.ConnectionError:
            time.sleep(1)


class Synapse(redis.StrictRedis):
    '''
        Wrapper to Redis that JSON en/decodes all values
    '''
    pool = redis.ConnectionPool(decode_responses=True)
    
    def get_unique_id(self, path='synapse:unique_id'):
        '''
            Return a unique integer ID (incremented at each call)
            optional path where to store last id.
        '''
        return self.incr(path)

    def pipeline(self, transaction=True, shard_hint=None):
        return SynapsePipeline(
            self.connection_pool,
            self.response_callbacks,
            transaction,
            shard_hint)

    def __init__(self):
        super(Synapse, self).__init__(connection_pool=self.pool)
        
        # Decorate response_callbacks to parse Json output
        FROM_JSON_CALLBACKS = {
                'GET':           self.parse_single_object,
                'HGETALL':       self.parse_hgetall,
                'HGET':          self.parse_single_object,
                'HKEYS':         self.parse_list,
                'HVALS':         self.parse_list,
                'SMEMBERS':      self.parse_smembers, 
                'ZRANGE':        self.parse_zrange,
                'ZRANGEBYSCORE': self.parse_zrange,
                'LRANGE':        self.parse_list,
                'LPOP':          self.parse_single_object,
                'BLPOP':         self.parse_bpop,
                'BRPOP':         self.parse_bpop,
        }
        
        def decorate_callback(fn, otherFn):
            def newFn(response, **options):
                new_response = fn(response, **options)
                return otherFn(new_response, **options)
            return newFn

        for cmd in FROM_JSON_CALLBACKS:
            if cmd in self.response_callbacks:
                self.response_callbacks[cmd] = decorate_callback(self.response_callbacks[cmd], FROM_JSON_CALLBACKS[cmd]) 
            else:
                self.response_callbacks[cmd] = FROM_JSON_CALLBACKS[cmd]

        self.pipe = self.pipeline()

    def parse_single_object(self, response, **options):
        if response == None:
            return None
        return json.loads(response)

    def set(self, key, value, *args, **kwargs):
        ''' JSON encodes object and stores it '''
        return super(Synapse, self).set( key, json.dumps(value, sort_keys=True), *args, **kwargs )
    
    def sget(self, key):
        '''
        Smart get: like get but smarter, returns good type:
            if redis hash, returns python dict
            if redis array, returns python array
            if redis set, return python set
            if redis string, returns python string
        '''
        if not self.exists(key):
            return None
        return {
                   'set': self.smembers,
                   'hash': self.hgetall,
                   'string': self.get,
                   'list': self.lmembers,
               }[self.type(key)](key)
        
#   NOT TESTED AT ALL !!! TODO: test this !
    def sset(self, key, value):
        '''
        Smart set: like set but smarter, sets good type:
            if python dict, uses redis hash
            if python array, uses redis array 
            if python set, uses redis set 
            otherwise uses redis string
        '''
        with self.pipeline() as pipe:
            pipe.delete(key)
     
            value_type = type(value).__name__
            if value_type == 'set':
                pipe.sadd(key, *list(json.dumps(v, sort_keys=True) for v in value) )
            elif value_type == 'list':
                pipe.rpush(key, *list(json.dumps(v, sort_keys=True) for v in value) )
            elif value_type == 'dict':
                pipe.hmset(key, {k: json.dumps(v, sort_keys=True) for k,v in value.items()})
            else:
                pipe.set(key, json.dumps(value, sort_keys=True))
     
            pipe.execute()

    # Hashes: fields can be objects
    def parse_hgetall(self, response, **options):
        return { json.loads(k): json.loads(v) for k, v in response.items() }

    def hget(self, key, field):
        return super(Synapse, self).hget(key, json.dumps(field, sort_keys=True))

    def hmget(self, key, *fields):
        return super(Synapse, self).hmget(key, *list(json.dumps(field, sort_keys=True) for field in fields))

    def hexists(self, key, field):
        return super(Synapse, self).hexists(key, json.dumps(field, sort_keys=True))

    def hincrby(self, key, field, increment):
        return super(Synapse, self).hincrby(key, json.dumps(field, sort_keys=True), increment)

    def hincrbyfloat(self, key, field, increment):
        return super(Synapse, self).hincrbyfloat(key, json.dumps(field, sort_keys=True), increment)

    def hdel(self, key, *fields):
        return super(Synapse, self).hdel(key, *list(json.dumps(field, sort_keys=True) for field in fields) )

    def hset(self, key, field, value):
        return super(Synapse, self).hset(key, json.dumps(field, sort_keys=True), json.dumps(value, sort_keys=True))

    def hsetnx(self, key, field, value):
        return super(Synapse, self).hsetnx(key, json.dumps(field, sort_keys=True), json.dumps(value, sort_keys=True))

    def hmset(self, key, mapping):
        return super(Synapse, self).hset( key, ([ json.dumps(field, sort_keys=True), json.dumps(value, sort_keys=True)] for field, value in mapping) )

    # Sets
    def sismember(self, key, value):
        return super(Synapse, self).sismember(key, json.dumps(value, sort_keys=True))

    def sadd(self, key, *args):
        return super(Synapse, self).sadd(key, *list(json.dumps(v, sort_keys=True) for v in args))

    def srem(self, key, *args):
        return super(Synapse, self).srem(key, *list(json.dumps(v, sort_keys=True) for v in args))
        
    def smembers(self, *args, **kwargs):
        return set(super(Synapse, self).smembers(*args, **kwargs))

    def smembers_as_list(self, *args, **kwargs):
        return super(Synapse, self).smembers(*args, **kwargs)

    def parse_smembers(self, response, **options):
        # return list as members may not be hashable, smember fct will turn in into set. 
        # caller should call smembers_as_list if it is known that members may be unhashable and deal with a list instead of a set
        return [json.loads(v) for v in response] 

    # oredered sets
    def zadd(self, key, *args):
        json_args = []

        a=iter(args)
        for score, value in zip(a,a):
            json_args.append( score )
            json_args.append( json.dumps(value, sort_keys=True) )
        
        return super(Synapse, self).zadd(key, *json_args)

    def zrem(self, key, *args):
        return super(Synapse, self).zrem(key, *list(json.dumps(v, sort_keys=True) for v in args))
        
    def zmembers(self, key):
        return self.zrange(key, 0, -1)

    def zscore(self, key, value):
        return super(Synapse, self).zscore(key, json.dumps(value, sort_keys=True))

    def parse_zrange(self, response, **options):
        if options.get('withscores', False):
            return [(json.loads(v[0]), v[1]) for v in response]
        else:
            return [json.loads(v) for v in response]

    # Lists
    def lmembers(self, key):
        return self.lrange(key, 0, -1)
    
    def parse_list(self, response, **options):
        return [json.loads(v) for v in response]

    def lpush(self, key, *args):
        return super(Synapse, self).lpush(key, *list(json.dumps(v, sort_keys=True) for v in args))
    
    def parse_bpop(self, response, **options):
        if response == None:
            return None
        return (response[0], json.loads(response[1]))

    def rpush(self, key, *args):
        return super(Synapse, self).rpush(key, *list(json.dumps(v, sort_keys=True) for v in args))
    
    def rpop(self, key):
        data = super(Synapse, self).rpop(key)
        if data == None:
            return None
        return json.loads(data)

    def publish(self, channel, msg):
        return super(Synapse, self).publish(channel, json.dumps(msg, sort_keys=True))

    def type(self, key):
        return super(Synapse, self).type(key)
    
    def pubsub(self):
        return super(Synapse, self).pubsub()

class SynapsePipeline(redis.client.BasePipeline, Synapse):
    "Pipeline for the Synapse class"
    pass



class TimeOutException(Exception):
    pass

import paho.mqtt.client as mqtt
from paho.mqtt import publish

class  Dendrite(mqtt.Client):
    '''
        Dendrite is used to retrieve configuration (and configuration updates), declare what RPC are processed by the calling module and make RPC calls
        Instantiate class, call subscribe, subscribe_conf, publish, publish_conf, call or provide with topic/service and callback. callback receive message and optionnaly topic. 
        
        Use class method publish_single for a single publish. This will connecet, publish and disconnect and will be blocking.
        
        When using instance:
        - all callbacks are executed in an other thread.
        - call are non blocking (publish will happen in another thread)
        
    '''
    CONF_PATH_PREFIX = 'conf/'
    
    SERVICE_REQUESTS_PATH_PREFIX = 'service/requests/'
    SERVICE_ANSWERS_PATH_PREFIX  = 'service/answers/'
    
    RPC_REQUESTS_PATH_PREFIX = 'rpc/requests/'
    RPC_ANSWERS_PATH_PREFIX  = 'rpc/answers/'
    
    MQTT_HOST = '127.0.0.1'
    MQTT_PORT = 1883
    
    def __init__(self):
        super().__init__()
        
        self.connect_async(self.MQTT_HOST, self.MQTT_PORT)
        self.loop_start()
        
        self.topics = set()
        
    @classmethod
    def publish_single(cls, topic, data, retain=False):
        return publish.single(topic, json.dumps(data), qos = 1, retain=retain, hostname=cls.MQTT_HOST, port=cls.MQTT_PORT)
    

    def finish(self):
        self.disconnect()
        self.loop_stop()
    
    def on_connect(self, mqttc, obj, flags, rc):
        if self.topics:
            self._subscribe([ (topic, 1) for topic in self.topics ])
        
            
    def _subscribe_cb_wrapper(self, fn):
        def wrapper(mqttc, userdata, message):
            data = json.loads(message.payload.decode())
            # Accept to send only data without topic
            if len(inspect.signature(fn).parameters) == 1:
                return fn(data)
            else: 
                return fn(data, message.topic)
        return wrapper

    def _subscribe_conf_cb_wrapper(self, fn):
        def wrapper(data, topic):
            subtopic = topic[len(self.CONF_PATH_PREFIX):]
            if len(inspect.signature(fn).parameters) == 1:
                return fn(data)
            else: 
                return fn(data, subtopic)
        return wrapper

    def _provide_cb_wrapper(self, fn):
        def wrapper(data, topic):
            subtopic = topic[len(self.SERVICE_REQUESTS_PATH_PREFIX):]
            correlation_id = data['request_id']
            data = data['data']
            if len(inspect.signature(fn).parameters) == 1:
                result = fn(data)
            else: 
                result = fn(data, subtopic)
            self.publish(self.SERVICE_ANSWERS_PATH_PREFIX + correlation_id, result)
            return result
        return wrapper

    def _provide_rpc_cb_wrapper(self, fn):
        def wrapper(data, topic):
            subtopic = topic[len(self.RPC_REQUESTS_PATH_PREFIX):]
            correlation_id = data['request_id']
            data = data['data']
            if len(inspect.signature(fn).parameters) == 1:
                result = fn(data)
            else: 
                result = fn(data, subtopic)
            self.publish(self.RPC_ANSWERS_PATH_PREFIX + correlation_id, result)
            return result
        return wrapper

    def subscribe(self, topic, cb):
        self.topics.add(topic)
        self.message_callback_add(topic, self._subscribe_cb_wrapper(cb))
        return self._subscribe(topic)

    def _subscribe(self, topic):
        return super().subscribe(topic, qos=1)
        
    def unsubscribe(self, topic):
        self.topics.discard(topic)
        self.message_callback_remove(topic)
        return super().unsubscribe(topic)
    
    def subscribe_conf(self, topic, cb):
        return self.subscribe(self.CONF_PATH_PREFIX + topic, self._subscribe_conf_cb_wrapper(cb))
    
    def publish(self, topic, data, retain=False):
        '''
            publish message to topic.
            By default, does not ask to retain message.
        '''
        return super().publish(topic, json.dumps(data), retain=retain, qos=1)

    def publish_conf(self, path, message):
        return self.publish(self.CONF_PATH_PREFIX+path, message, retain=True)
    
    def provide(self, service, cb):
        '''
        provides a service RPC style by running callback cb on call.
        '''
        return self.subscribe(self.SERVICE_REQUESTS_PATH_PREFIX + service, self._provide_cb_wrapper(cb))

    def unprovide(self, service):
        return self.unsubscribe(self.SERVICE_REQUESTS_PATH_PREFIX + service)
    
    def provide_rpc(self, service, cb):
        '''
        provides a service RPC style by running callback cb on call.
        '''
        return self.subscribe(self.RPC_REQUESTS_PATH_PREFIX + service, self._provide_rpc_cb_wrapper(cb))

    def unprovide_rpc(self, service):
        return self.unsubscribe(self.RPC_REQUESTS_PATH_PREFIX + service)

    def call(self, service, data=None, timeout=30):
        '''
        RPC request to service, returns result or raises TimeOutException.
        This should not be called in async function as it will block the event loop.
        However it can be safely called from Thread Executor or when not event loop is running (will run it for a short while)
        '''
        correlation_id = str(uuid.uuid4())
        future = concurrent.futures.Future()
        
        def cb(result):
            future.set_result(result)
            self.unsubscribe(self.RPC_ANSWERS_PATH_PREFIX + correlation_id)
            
        self.subscribe(self.RPC_ANSWERS_PATH_PREFIX + correlation_id, cb)
        self.publish(self.RPC_REQUESTS_PATH_PREFIX + service, {'request_id': correlation_id, 'data': data})
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            raise TimeOutException('Call to "{service}" timed out after {timeout} seconds'.format(service=service, timeout=timeout))
        
    def call_provided(self, service, data=None, timeout=30):
        '''
        local RPC to a procided service, returns result or raises TimeOutException.
        This should not be called in async function as it will block the event loop.
        However it can be safely called from Thread Executor or when not event loop is running (will run it for a short while)
        '''
        correlation_id = str(uuid.uuid4())
        future = concurrent.futures.Future()
        
        def cb(result):
            future.set_result(result)
            self.unsubscribe(self.RPC_ANSWERS_PATH_PREFIX + correlation_id)
            
        self.subscribe(self.SERVICE_ANSWERS_PATH_PREFIX + correlation_id, cb)
        self.publish(self.SERVICE_REQUESTS_PATH_PREFIX + service, {'request_id': correlation_id, 'data': data})
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            raise TimeOutException('Call to "{service}" timed out after {timeout} seconds'.format(service=service, timeout=timeout))
    
        
    def wait_complete(self, timeout=None):
        '''
        Block until timeout or disconnect called
        '''
        self._thread.join(timeout)
    
    
    
    