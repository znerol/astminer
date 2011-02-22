#!/usr/bin/env python
from pyactiveresource.activeresource import ActiveResource
from twisted.internet.defer import Deferred, DeferredQueue
from twisted.internet.threads import deferToThread
from twisted.python import log
import time

class Issue(ActiveResource):
    pass

class CallTracker(object):

    def __init__(self, uniqueid, callerid, templates, usermap):
        self.callerid = callerid
        self.uniqueid = uniqueid
        self.templates = templates
        self.usermap = usermap

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
        self._issue_placeholders['queueMember'] = self.queueMember
        self._issue_placeholders['callAnswered'] = self.callAnswered

        changed = False
        try:
            userid = int(self.usermap[self.queueMember])
            self._issue_placeholders['assigned_to_id'] = userid
            changed = self._mergeIssueWithTemplate("IssueAssign")
        except KeyError:
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
        usermap = dict(self.config.items("UserMap"))
        callTracker = CallTracker(uniqueid, callerid, self.config, usermap)
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

# copy&paste from starpy/manager.py, converted to ReconnectingClientFactory
from twisted.internet import protocol, reactor
from starpy import manager
class MyAMIFactory(protocol.ReconnectingClientFactory):
    """A reconnecting factory for AMI protocols"""
    protocol = manager.AMIProtocol
    maxDelay = 300 # 5 minutes

    def __init__(self, username, secret, app):
        self.username = username
        self.secret = secret

        # Another hack to get application up and running after ami login.
        # Normally we'd implement that in the subclass of AMIProtocol...
        self.app = app

    def startedConnecting(self, connector):
        """XXX: Work around messy implementation in starpy"""
        self.loginDefer = Deferred()
        self.loginDefer.addCallback(self.loginComplete)

    def loginComplete(self, ami):
        """Reset retry delay after a successfull login"""
        self.resetDelay()
        self.app.amiConnectionMade(ami)

def makeService(twisted_config):
    from ConfigParser import RawConfigParser
    from starpy import fastagi
    from twisted.application import internet

    # Read configuration
    f = twisted_config['config']
    config = RawConfigParser()
    config.readfp(open(f), f)

    # Set Redmine REST API connection parameters
    Issue.set_site(config.get('Astminer', 'RedmineSite'))
    Issue.set_user(config.get('Astminer', 'RedmineUser'))
    Issue.set_password(config.get('Astminer', 'RedminePassword'))

    app = Application(config)

    # Connect to asterisk manager interface
    f = MyAMIFactory(
            config.get('Astminer', 'ManagerUser'),
            config.get('Astminer', 'ManagerPassword'),
            app)
    reactor.connectTCP(
            config.get('Astminer', 'ManagerHost'),
            int(config.get('Astminer', 'ManagerPort')),
            f)

    # Setup FastAGI listener
    f = fastagi.FastAGIFactory(app.agiRequestReceived)
    service = internet.TCPServer(
            int(config.get('Astminer', 'AgiPort')), f)

    return service
