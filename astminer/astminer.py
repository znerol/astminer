#!/usr/bin/env python
from pyactiveresource.activeresource import ActiveResource
from twisted.internet.defer import Deferred, DeferredQueue
from twisted.internet.threads import deferToThread
from twisted.python import log
import time

class Issue(ActiveResource):
    pass

class User(ActiveResource):
    pass

class CallTracker(object):

    def __init__(self, uniqueid, callerid, templates):
        self.callerid = callerid
        self.uniqueid = uniqueid
        self.templates = templates

        self.startTime = None
        self.stopTime = None
        self.callDuration = None
        self.callAnswered = False
        self.talkDuration = None
        self.talkStart = None
        self.queueMember = None

        self.issue = None
        self._issue_placeholders = {}

        self._ar_queue = DeferredQueue()
        self._ar_queue_poll()

    def start(self):
        self.startTime = time.time()

        # create ticket
        f = self._blockingCreateIssue
        log.msg("Submit: %s" % f)
        self._ar_queue_submit(f).addCallback(self._logBlockingCompleted, f)

    def answer(self, member):
        self.talkStart = time.time()
        self.callAnswered = True
        self.queueMember = member

        # map and assign ticket to member
        f = self._blockingAssignIssue
        log.msg("Submit: %s" % f)
        self._ar_queue_submit(f).addCallback(self._logBlockingCompleted, f)

    def hangup(self):
        self.endTime = time.time()
        self.callDuration = self.endTime - self.startTime

        if (self.callAnswered):
            self.talkDuration = self.endTime - self.talkStart

        # update ticket status here
        f = self._blockingUpdateHangupIssue
        log.msg("Submit: %s" % f)
        self._ar_queue_submit(f).addCallback(self._logBlockingCompleted, f)

    def _logBlockingCompleted(self, result, f):
        log.msg("Completed: %s" % f)

    def _mergeIssueWithTemplate(self, tmpl_name):
        changed = False
        if self.templates.has_section(tmpl_name):
            for (k,v) in self.templates.items(tmpl_name):
                setattr(self.issue, k, v % self._issue_placeholders)
                changed = True

        tmpl_custom_fields_name = tmpl_name + "/custom_fields"
        if self.templates.has_section(tmpl_custom_fields_name):
            custom_fields = []
            for (k, v) in self.templates.items(tmpl_custom_fields_name):
                custom_fields.append({
                    'id': k,
                    'value': v % self._issue_placeholders
                    })
            if len(custom_fields):
                self.issue.custom_fields = custom_fields
                changed = True

        return changed

    def _blockingCreateIssue(self):
        self._issue_placeholders['callerid'] = self.callerid
        self._issue_placeholders['uniqueid'] = self.uniqueid
        self._issue_placeholders['startTime'] = self.startTime

        self.issue = Issue()
        self._mergeIssueWithTemplate("IssueCreate")

        self.issue.save()

    def _blockingAssignIssue(self):
        # Support Queue Members in the form "sip/username01":
        # * Strip protocol prefix -> username01
        # * Derive a form without trailing digits -> username
        # Like this we are able to map multiple sip accounts to one redmine
        # account if desired.
        username = self.queueMember.split('/', 1)[1]
        username_nodigits = username.rstrip('0123456789')

        self._issue_placeholders['queueMember'] = self.queueMember
        self._issue_placeholders['callAnswered'] = self.callAnswered

        # AFAIK Redmine does not support querying the users database with a
        # condition, so we have to load all users and match them against our
        # username condition manually.
        found = False
        users = User.find()
        for user in users:
            if user.login in (username, username_nodigits):
                self._issue_placeholders['assigned_to_id'] = int(user.id)
                found = True
                break

        changed = False
        if found:
            changed = self._mergeIssueWithTemplate("IssueAssign")
        else:
            changed = self._mergeIssueWithTemplate("IssueUserNotFound")

        if changed:
            self.issue.save()

    def _blockingUpdateHangupIssue(self):
        self._issue_placeholders['stopTime'] = self.stopTime
        self._issue_placeholders['callDuration'] = self.callDuration
        self._issue_placeholders['talkDuration'] = self.talkDuration
        changed = False
        if self.callAnswered:
            changed = self._mergeIssueWithTemplate("IssueHangupAnswered")
        else:
            changed = self._mergeIssueWithTemplate("IssueHangupNotAnswered")

        if changed:
            self.issue.save()

    def _ar_queue_submit(self, f, *args, **kwds):
        d = Deferred()
        invocation = (f, args, kwds, d)
        self._ar_queue.put(invocation)

        return d

    def _ar_queue_poll(self, result=None):
        d = self._ar_queue.get()
        d.addCallbacks(self._ar_queue_invoke, log.err)

    def _ar_queue_invoke(self, invocation):
        f, args, kwds, d2 = invocation
        d = deferToThread(f, *args, **kwds)
        d.addCallbacks(self._ar_queue_poll, log.err)
        d.chainDeferred(d2)

    def __repr__(self):
        return "CallTracker(\"%s\", \"%s\")" % (self.uniqueid, self.callerid)

class Application(object):
    """Application for the call duration callback mechanism"""

    def __init__(self, config):
        self.trackers = {}
        self.config = config

    def agiRequestReceived(self, agi):
        uniqueid = agi.variables['agi_uniqueid']
        callerid = agi.variables['agi_callerid']
        callTracker = CallTracker(uniqueid, callerid, self.config)
        callTracker.start()
        self.trackers[uniqueid] = callTracker

        agi.finish()
        return agi

    def amiConnectionMade(self, ami):
        def amiChannelHangup(ami, event):
            uniqueid = event['uniqueid']
            try:
                callTracker = self.trackers.pop(uniqueid)
                callTracker.hangup()
            except KeyError:
                pass

        def amiAgentConnected(ami, event):
            uniqueid = event['uniqueid']
            try:
                callTracker = self.trackers[uniqueid]
                callTracker.answer(event['member'])
            except KeyError:
                pass

        ami.registerEvent('Hangup', amiChannelHangup)
        ami.registerEvent('AgentConnect', amiAgentConnected)

        return ami

def makeService(twisted_config):
    from ConfigParser import RawConfigParser
    from starpy import manager, fastagi
    from twisted.application import internet

    # Read configuration
    f = twisted_config['config']
    config = RawConfigParser()
    config.readfp(open(f), f)

    # Set Redmine REST API connection parameters
    User.set_site(config.get('Astminer', 'RedmineSite'))
    User.set_user(config.get('Astminer', 'RedmineUser'))
    User.set_password(config.get('Astminer', 'RedminePassword'))
    Issue.set_site(config.get('Astminer', 'RedmineSite'))
    Issue.set_user(config.get('Astminer', 'RedmineUser'))
    Issue.set_password(config.get('Astminer', 'RedminePassword'))

    app = Application(config)

    # Connect to asterisk manager interface
    theManager = manager.AMIFactory(
            config.get('Astminer', 'ManagerUser'),
            config.get('Astminer', 'ManagerPassword'))
    m = theManager.login(
            config.get('Astminer', 'ManagerHost'),
            int(config.get('Astminer', 'ManagerPort')), 10)
    m.addCallback(app.amiConnectionMade)

    # Setup FastAGI listener
    f = fastagi.FastAGIFactory(app.agiRequestReceived)
    service = internet.TCPServer(
            int(config.get('Astminer', 'AgiPort')), f)

    return service
