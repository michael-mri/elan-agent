from django import forms
from django.conf import settings
from django.contrib.sites.models import get_current_site
from django.core.urlresolvers import reverse
from django.core.validators import validate_ipv4_address, validate_ipv6_address
from django.http import HttpResponseRedirect
from django.shortcuts import render, redirect
from django.template import RequestContext, loader
from django.utils.translation import ugettext as _
from django.views.decorators.cache import never_cache
import time

from elan import nac, utils
from elan import session
from elan.authentication import pwd_authenticate
from elan.captive_portal import GUEST_ACCESS_CONF_PATH, submit_guest_request, is_authz_pending, Administrator, ELAN_AGENT_FQDN, CAPTIVE_PORTAL_FQDN, ELAN_AGENT_FQDN_IP, ELAN_AGENT_FQDN_IP6, CAPTIVE_PORTAL_FQDN_IP, CAPTIVE_PORTAL_FQDN_IP6
from elan.event import Event
from elan.mail import send_mail
from elan.network import NetworkConfiguration
from elan.neuron import Synapse, Dendrite, RequestTimeout, RequestError
from elan.utils import get_ip4_addresses, get_ip6_addresses, ip4_to_mac, is_iface_up, physical_ifaces

ADMIN_SESSION_IDLE_TIMEOUT = 300  # seconds


def requirePortalURL(fn):
    '''
    View decorator to make sure url used is the one of the agent and not the target URL
    '''
    if settings.DEBUG:
        return fn

    def wrapper(request, *args, **kwargs):
        agent_ips = [ ip['address'].lower() for ip in (get_ip4_addresses('br0') + get_ip6_addresses('br0')) ]
        allowed_sites = agent_ips + [CAPTIVE_PORTAL_FQDN, ELAN_AGENT_FQDN, ELAN_AGENT_FQDN_IP, ELAN_AGENT_FQDN_IP6, CAPTIVE_PORTAL_FQDN_IP, CAPTIVE_PORTAL_FQDN_IP6]
        if str(get_current_site(request)).replace('[', '').replace(']', '').lower() not in allowed_sites:
            return redirect2status(request)
        return fn(request, *args, **kwargs)

    return wrapper


def redirect2status(request):
    if 'dashboard' in request.META or settings.DEBUG and 'dashboard' in request.GET:
        host = request.META.get('HTTP_HOST', ELAN_AGENT_FQDN)
        agent_ips = [ip['address'] for ip in utils.get_ip4_addresses() + utils.get_ip6_addresses()]
        if host in agent_ips:
            # if trying to get to agent with ip, redirect to IP
            redirect_fqdn = host
        else:
            redirect_fqdn = ELAN_AGENT_FQDN

        if settings.DEBUG:
            redirect_fqdn = host

        return HttpResponseRedirect('https://' + redirect_fqdn + reverse('dashboard'))
    return HttpResponseRedirect('https://' + CAPTIVE_PORTAL_FQDN + reverse('status'))


@requirePortalURL
@never_cache
def status(request):
    clientIP = request.META['REMOTE_ADDR']
    clientMAC = ip4_to_mac(clientIP)

    if not is_authenticated(clientMAC):
        return redirect('login')

    # if looking for edgeagent, redirect to it...
    if str(get_current_site(request)) in [ELAN_AGENT_FQDN, ELAN_AGENT_FQDN_IP, ELAN_AGENT_FQDN_IP6]:
        return HttpResponseRedirect('http://' + ELAN_AGENT_FQDN + reverse('dashboard'))

    context = {}

    if 'web_authentication' in request.META:
        context['web_authentication'] = request.META['web_authentication']

    if 'guest_access' in request.META:
        context['guest_access'] = Synapse().hget(GUEST_ACCESS_CONF_PATH, int(request.META['guest_access']))

    return render(request, 'captive-portal/status.html', context)


def is_authenticated(mac):
    return bool(session.get_authentication_sessions(mac, source='captive-portal-web') or session.get_authentication_sessions(mac, source='captive-portal-guest'))


@requirePortalURL
def login(request, context=None):
    if context is None:
        context = {}

    clientIP = request.META['REMOTE_ADDR']
    clientMAC = ip4_to_mac(clientIP)

    if is_authenticated(clientMAC):
        # VlanID not present, means it has been not been redirected, so MAC is allowed on VLAN (maybe not from web or captive portal)
        return redirect('status')

    default_context = {}
    if 'web_authentication' in request.META:
        default_context[ 'web_authentication'] = request.META['web_authentication']

    if 'guest_access' in request.META:
        default_context['guest_access'] = Synapse().hget(GUEST_ACCESS_CONF_PATH, int(request.META['guest_access']))
        if default_context['guest_access']:
            default_context.update(
                        guest_registration_fields=default_context['guest_access']['registration_fields'],
                        guest_access_pending=is_authz_pending(clientMAC)
            )

    for key in default_context:
        if key not in context:
            context[key] = default_context[key]

    if request.method != 'POST' or context.get('post_processed', False):
        return render(request, 'captive-portal/login.html', context)

    # POST
    try:
        username = request.POST['username']
        password = request.POST['password']
    except (KeyError):
        # Redisplay the login form.
        context['error_message'] = _("'username' or 'password' missing.")
        return render(request, 'captive-portal/login.html', context)

    context['username'] = username
    if 'web_authentication' not in request.META or not pwd_authenticate(request.META['web_authentication'], username, password, source='captive-portal-web'):
        context['error_message'] = _("Invalid username or password.")
        return render(request, 'captive-portal/login.html', context)

    vlan = '{interface}.{id}'.format(interface=request.META['interface'], id=request.META['vlan_id'])
    # start session
    # TODO: use effective auth_provider, this one could be a group
    authz = nac.newAuthz(clientMAC, source='captive-portal-web', till_disconnect=True,
                         login=username, authentication_provider=request.META['web_authentication'],
                         vlan=vlan
    )
    # TODO: if vlan incorrect, try to change it
    if not authz or vlan not in authz.allow_on:
        # log no assignment rule matched....
        event = Event('device-not-authorized', source='captive-portal-web', level='danger')
        event.add_data('mac', clientMAC, 'mac')
        event.add_data('vlan', vlan)
        event.add_data('authentication_provider', request.META['web_authentication'], 'authentication')
        event.add_data('login', username)
        event.notify()

    return redirect('status')


@requirePortalURL
def logout(request):
    clientIP = request.META['REMOTE_ADDR']
    clientMAC = ip4_to_mac(clientIP)
    nac.checkAuthz(clientMAC, remove_source='captive-portal-web', end_reason='logout')

    return redirect('login')


class DynamicFieldForm(forms.Form):
    FIELD_MAPPING = {
            'text': forms.CharField,
            'textarea': forms.CharField,
            'date': forms.DateField,
            'date-time': forms.DateTimeField,
            'time': forms.TimeField,
            'email': forms.EmailField,
    }

    def __init__(self, *args, **kwargs):
        fields = kwargs.pop('fields')
        super().__init__(*args, **kwargs)

        for field in fields:
            self.fields[field['name']] = self.FIELD_MAPPING[field['type']](required=field.get('required'))
            validation_patterns = field.get('validation_patterns', [])
            if validation_patterns:
                self.create_validator(field['name'], validation_patterns)

    def create_validator(self, name, patterns):
        import fnmatch

        def field_validator(form):
            value = form.cleaned_data[name]
            for pattern in patterns:
                if fnmatch.fnmatch(value, pattern):
                    return value
            raise forms.ValidationError(_("Field does not match one of the patterns ({})").format(', '.join(patterns)))

        setattr(self, 'clean_{}'.format(name), field_validator.__get__(self))


def get_request_form(guest_registration_fields, guest_access_conf, data=None):
    form_fields = [
            {
                    'name':'field-{}'.format(f['id']),
                    'required':f.get('required', True),
                    'type':f.get('type')
            } for f in guest_registration_fields
    ]
    form_fields.append({'name':'guest_access_modification_time', 'required': True, 'type': 'text'})
    if guest_access_conf.get('validation_patterns', None):
        form_fields.append({'name':'sponsor_email', 'required':True, 'validation_patterns': guest_access_conf['validation_patterns'], 'type': 'email'})
    return DynamicFieldForm(data, fields=form_fields)


@requirePortalURL
def guest_access(request):
    clientIP = request.META['REMOTE_ADDR']
    clientMAC = ip4_to_mac(clientIP)

    if request.method != 'POST' or is_authenticated(clientMAC):
        return redirect('login')

    interface = request.META['interface']
    vlan_id = request.META['vlan_id']
    guest_access = int(request.META['guest_access'])
    synapse = Synapse()

    # Guest access fields
    guest_access_conf = synapse.hget(GUEST_ACCESS_CONF_PATH, guest_access)
    guest_registration_fields = guest_access_conf['registration_fields']

    form = get_request_form(guest_registration_fields, guest_access_conf, request.POST)

    if form.is_valid():
        guest_request = dict(
                mac=clientMAC,
                interface=interface,
                vlan_id=vlan_id,
                fields=[],
                guest_access=guest_access,
                sponsor_email=form.cleaned_data.get('sponsor_email', ''),
                guest_access_modification_time=form.cleaned_data.get('guest_access_modification_time')
        )
        for field in guest_registration_fields:
            guest_request['fields'].append(dict(
                                         display_name=field['display_name'],
                                         type=field['type'],
                                         value=form.cleaned_data.get('field-{}'.format(field['id']), ''),
            ))

        try:
            guest_request = submit_guest_request(guest_request)
            if guest_access_conf['type'] == 'sponsored':
                # send mail
                html_template = loader.get_template('captive-portal/guest-request-email.html')
                text_template = loader.get_template('captive-portal/guest-request-email.txt')
                context = RequestContext(request, {
                    'guest_request': guest_request,
                })
                html = html_template.render(context)
                text = text_template.render(context)

                if not guest_request['sponsor_email']:
                    recipients = guest_access_conf['fixed_recipients']
                    bcc_recipients = []
                else:
                    recipients = [guest_request['sponsor_email']]
                    bcc_recipients = guest_access_conf['fixed_recipients']

                send_mail(recipients=recipients,
                            bcc_recipients=bcc_recipients,
                            html=html,
                            text=text,
                            mail_subject='Guest Request for Network Access'
                )

            return redirect('status')
        except:
            # No ID
            if 'non_field_errors' not in form.errors:
                form.errors['non_field_errors'] = []
            form.errors['non_field_errors'].append(_('Error during request, please try again or contact administrator.'))

    # Form not valid or error when getting Guest Request Id
    # we update the field conf with passed value and error messages as it is easier to display in Template (difficult to access error.var where var='field-{id}' and to build that access in the template)
    for field in guest_registration_fields:
        field.update(value=request.POST.get('field-{}'.format(field['id']), ''))
        field.update(errors=form.errors.get('field-{}'.format(field['id']), []))

    context = {'guest_registration_fields': guest_registration_fields, 'guest_request_form': form }
    context['post_processed'] = True
    return login(request, context)


# Session function helpers
def admin_session_login(session, login):
    session['admin'] = login
    session.set_expiry(ADMIN_SESSION_IDLE_TIMEOUT)


def admin_session_logout(session):
    session.clear()
    session.flush()


def require_admin(fn):

    def wrapper(request, *args, **kwargs):
        # when not registered, allow all, else required admin session
        if not is_registered() or request.session.get('admin', None):
            return fn(request, *args, **kwargs)
        return redirect('dashboard')

    return wrapper


def require_post(fn):

    def wrapper(request, *args, **kwargs):
        if request.method != 'POST':
            return redirect2status(request)
        return fn(request, *args, **kwargs)

    return wrapper


def save_admin_session(fn):

    def wrapper(request, *args, **kwargs):
        if request.session.get('admin', None):
            request.session.modified = True
        return fn(request, *args, **kwargs)

    return wrapper


def is_registered():
    # consider registered if at leat one admin has been set
    return bool(Administrator.count())


@requirePortalURL
@save_admin_session
def dashboard(request, context=None):
    if context is None:
        context = {}

    dendrite = Dendrite()

    registered = is_registered()

    try:
        # will raise error if not connected
        dendrite.call('check-connectivity', timeout=2)
        is_connected = True
        connectivity_error = ''
    except RequestTimeout:
        is_connected = None  # Unknown
        connectivity_error = 'Connectivity check not implemented'
    except RequestError as e:
        is_connected = False
        connectivity_error = e.error_str

    registration_available = False
    registration_error = ''
    if not registered:
        try:
            dendrite.call('register', timeout=2)
            registration_available = True
        except RequestTimeout:
            registration_available = False
            registration_error = 'Registration service not implemented'
        except RequestError as e:
            registration_available = False
            registration_error = e.error_str

    context.update(
               registration_available=registration_available,
               registration_error=registration_error,
               is_admin=bool(request.session.get('admin', False)),
               is_connected=is_connected,
               connectivity_error=connectivity_error,
               is_registered=registered,
               interfaces={ iface: {'up': is_iface_up(iface)} for iface in physical_ifaces()},

               ipsv4=utils.get_ip4_addresses('br0'),
               ipv4_gw=utils.get_ip4_default_gateway(),
               ipv4_dns=utils.get_ip4_dns_servers(),

               ipsv6=utils.get_ip6_global_addresses('br0'),
               ipv6_gw=utils.get_ip6_default_gateway(),
               ipv6_dns=utils.get_ip6_dns_servers(),
    )
    if not context.get('location', ''):
        # TODO:
        context['location'] = ''

    ip_conf = NetworkConfiguration()
    if not context.get('ipv4_form', None):
        context['ipv4_form'] = Ip4ConfigurationForm(initial=ip_conf.ipv4)
    if not context.get('ipv6_form', None):
        context['ipv6_form'] = Ip6ConfigurationForm(initial=ip_conf.ipv6)

    return render(request, 'captive-portal/dashboard.html', context)


@requirePortalURL
def admin_logout(request):
    admin_session_logout(request.session)

    return redirect('dashboard')


@requirePortalURL
@require_post
def admin_login(request):
    context = {}
    post_dict = request.POST.dict()

    if not is_registered():
        dendrite = Dendrite()
        try:
            dendrite.call('register', post_dict)
        except RequestTimeout:
            context.update(form_errors={'non_field_errors': [_('Request timeout')]})
        except RequestError as e:
            if e.errors:
                context.update(form_errors=e.errors)
            else:
                context.update(form_errors={'non_field_errors': [e.error_str]})
        else:
            # Registration succeeded -> redirect to same to avoid repost
            admin_session_login(request.session, post_dict['login'])
            return redirect('dashboard')
    else:
        # Authenticate admin
        login = post_dict.get('login', None)
        if login:
            admin = Administrator.get(login)
            if admin and admin.check_password(request.POST.get('password', None)):
                admin_session_login(request.session, login)
                return redirect('dashboard')

        context['form_errors'] = [_('Invalid Credentials')]

    context.update(**post_dict)
    return dashboard(request, context)


class MultiIp4AddressField(forms.Field):

    def to_python(self, value):
        "Normalize data to a list of strings."

        # Return an empty list if no input was given.
        if not value:
            return []
        return value.replace(' ', '').split(',')

    def validate(self, value):
        "Check if value consists only of valid IPv4s."

        # Use the parent's handling of required fields, etc.
        super(MultiIp4AddressField, self).validate(value)

        for ip in value:
            validate_ipv4_address(ip)

    def prepare_value(self, value):
        if isinstance(value, list):
            return ', '.join(value)
        return value


class Ip4ConfigurationForm(forms.Form):
    type = forms.CharField(required=True)
    address = forms.GenericIPAddressField(protocol='IPv4', required=False)
    mask = forms.IntegerField(required=False, min_value=0, max_value=32)
    gateway = forms.GenericIPAddressField(protocol='IPv4', required=False)
    dns = MultiIp4AddressField(required=False)

    def clean_address(self):
        data = self.cleaned_data
        address = data.get('address', '')
        if data.get('type') == 'static' and not address:
            raise forms.ValidationError('Address is required for Static configuration')
        return address

    def clean_mask(self):
        data = self.cleaned_data
        mask = data.get('mask', '')
        if data.get('type') == 'static' and not mask:
            raise forms.ValidationError('Mask is required for Static configuration')
        return mask

    def clean_gateway(self):
        data = self.cleaned_data
        gateway = data.get('gateway', '')
        if data.get('type') == 'static' and not gateway:
            raise forms.ValidationError('Gateway is required for Static configuration')
        return gateway


class MultiIp6AddressField(forms.Field):

    def to_python(self, value):
        "Normalize data to a list of strings."
        # Return an empty list if no input was given.
        if not value:
            return []
        return value.replace(' ', '').split(',')

    def validate(self, value):
        "Check if value consists only of valid IPv4s."
        # Use the parent's handling of required fields, etc.
        super(MultiIp6AddressField, self).validate(value)
        for ip in value:
            validate_ipv6_address(ip)

    def prepare_value(self, value):
        if isinstance(value, list):
            return ', '.join(value)
        return value


class Ip6ConfigurationForm(forms.Form):
    type = forms.CharField(required=True)
    address = forms.GenericIPAddressField(protocol='IPv6', required=False)
    mask = forms.IntegerField(required=False, min_value=0, max_value=128)
    gateway = forms.GenericIPAddressField(protocol='IPv6', required=False)
    dns = MultiIp6AddressField(required=False)

    def clean_address(self):
        data = self.cleaned_data
        address = data.get('address', '')
        if data.get('type') == 'static' and not address:
            raise forms.ValidationError('Address is required for Static configuration')
        return address

    def clean_mask(self):
        data = self.cleaned_data
        mask = data.get('mask', '')
        if data.get('type') == 'static' and not mask:
            raise forms.ValidationError('Mask is required for Static configuration')
        return mask

    def clean_gateway(self):
        data = self.cleaned_data
        gateway = data.get('gateway', '')
        if data.get('type') == 'static' and not gateway:
            raise forms.ValidationError('Gateway is required for Static configuration')
        return gateway


@requirePortalURL
@require_post
@require_admin
@save_admin_session
def admin_ipv4_conf(request):
    form = Ip4ConfigurationForm(request.POST)
    if form.is_valid():
        Dendrite.publish_conf_single('ipv4', form.cleaned_data)
        time.sleep(5)  # wait for conf to apply
        return redirect('dashboard')

    return dashboard(request, context={'ipv4_form': form})


@requirePortalURL
@require_post
@require_admin
@save_admin_session
def admin_ipv6_conf(request):
    form = Ip6ConfigurationForm(request.POST)
    if form.is_valid():
        Dendrite.publish_conf_single('ipv6', form.cleaned_data)
        time.sleep(5)  # wait for ipv6 autoconf
        return redirect('dashboard')

    return dashboard(request, context={'ipv6_form': form})
