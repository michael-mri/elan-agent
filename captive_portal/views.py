from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render, redirect
import subprocess, re
from django.views.decorators.cache import never_cache
from django.contrib.sites.models import get_current_site
import socket
import fcntl
import struct
from origin.captive_portal import CONF_PATH
from origin.synapse import Synapse
from origin.authentication import pwd_authenticate
from origin import nac

def requirePortalURL(fn):
    '''
    View decorator to make sure url used is the one of the agent and not the target URL 
    '''
    def wrapper(request, *args, **kwargs):
        agent_ip = get_ip_address('br0')
        if str(get_current_site(request)) != agent_ip:
            return HttpResponseRedirect( 'http://' + agent_ip)
        return fn(request, *args, **kwargs)
    return wrapper



@requirePortalURL
def redirect2status(request):
    return redirect('status')

@requirePortalURL
@never_cache
def status(request):
    clientIP = request.META['REMOTE_ADDR']
    clientMAC = ip2mac(clientIP)
    if not nac.macAllowed(clientMAC, request.META['vlan_id']):
        return redirect('login')
    return HttpResponse('U R now connected !: ' + request.build_absolute_uri() + " -- " + str(get_current_site(request)) + " -- ")

@requirePortalURL
def login(request):
    vlan_id = request.META['vlan_id'] #debug
    if request.method == 'POST':
        try:
            username = request.POST['username']
            password = request.POST['password']
        except (KeyError):
            # Redisplay the login form.
            return render(request, 'captive_portal/login.html', { 'vlan_id': vlan_id, 'error_message': "'username' or 'password' missing.",})
        
        # Retrieve authenticator
        synapse = Synapse()
        authenticator_id = synapse.hget(CONF_PATH, request.META['web_authentication'])
        
        
        if not pwd_authenticate(authenticator_id, username, password):
            return render(request, 'captive_portal/login.html', { 'vlan_id': vlan_id, 'error_message': "Invalid username or password.", 'username': username})
    
        clientIP = request.META['REMOTE_ADDR']
        clientMAC = ip2mac(clientIP)
        nac.allowMAC(clientMAC, request.META['vlan_id'])
        return redirect('status')

    return render(request, 'captive_portal/login.html', {'vlan_id': vlan_id})

@requirePortalURL
def logout(request):
    clientIP = request.META['REMOTE_ADDR']
    clientMAC = ip2mac(clientIP)
    nac.disallowMAC(clientMAC, request.META['vlan_id'])
    #TODO: Flush connections with conntrack (get IPs of MAC and conntrack -D -s <IP>)
    
    return redirect('login')

def get_ip_address(ifname):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    return socket.inet_ntoa(fcntl.ioctl(
        s.fileno(),
        0x8915,  # SIOCGIFADDR
        struct.pack('256s', ifname[:15])
    )[20:24])
    
def ip2mac(ip):
    p = subprocess.Popen(['ip','neigh', 'show', ip], stdout=subprocess.PIPE)
    output = p.communicate()[0]
    m = re.search(r'[0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]', output)
    if m:
        return str(m.group(0))

