#!/usr/bin/env python
from twisted.internet import reactor
from twisted.internet.defer import DeferredQueue
from twisted.internet.threads import deferToThread
from starpy import manager, fastagi
from pyactiveresource.activeresource import ActiveResource
from ConfigParser import RawConfigParser
import logging
import sys
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
        log.info("%s: start" % self)

        # open ticket here
        self._ar_queue_submit(self._blockingCreateIssue)

    def setQueueMember(self, member):
        self.talkStart = time.time()
        self.callAnswered = True
        self.queueMember = member

        log.info("%s: member %s answered" % (self, member))

        # map and assign ticket to member
        self._ar_queue_submit(self._blockingAssignIssue)

    def hangup(self):
        self.endTime = time.time()
        self.callDuration = self.endTime - self.startTime

        log.info("%s: stop, total duration: %f" % (self, self.callDuration))
        if (self.callAnswered):
            self.talkDuration = self.endTime - self.talkStart
            log.info("%s: stop, talk duration: %f" % (self, self.talkDuration))
        else:
            log.info("%s: nobody answered this call", self)

        # update ticket status here
        self._ar_queue_submit(self._blockingUpdateHangupIssue)

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
        log.debug('Enter _blockingCreateIssue')
        self._issue_placeholders['callerid'] = self.callerid
        self._issue_placeholders['uniqueid'] = self.uniqueid
        self._issue_placeholders['startTime'] = self.startTime

        self.issue = Issue()
        self._mergeIssueWithTemplate("IssueCreate")

        self.issue.save()
        log.debug('Exit _blockingCreateIssue')

    def _blockingAssignIssue(self):
        log.debug('Enter _blockingAssignIssue')

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
                log.debug('Found user %d' % int(user.id))
                self._issue_placeholders['assigned_to_id'] = int(user.id)
                found = True
                break

        changed = False
        if found:
            changed = self._mergeIssueWithTemplate("IssueAssign")
        else:
            log.warning('Redmine user for queue member %s not found' % self.queueMember)
            changed = self._mergeIssueWithTemplate("IssueUserNotFound")

        if changed:
            self.issue.save()
        log.debug('Exit _blockingAssignIssue')

    def _blockingUpdateHangupIssue(self):
        log.debug('Enter _blockingUpdateHangupIssue')

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
        log.debug('Exit _blockingUpdateHangupIssue')

    def _ar_queue_submit(self, f, *args, **kwds):
        log.debug("_ar_queue_submit: %s", f)
        invocation = (f, args, kwds)
        self._ar_queue.put(invocation)

    def _ar_queue_poll(self, *ignored):
        log.debug("_ar_queue_poll -> deferred")
        d = self._ar_queue.get()
        d.addCallback(self._ar_queue_invoke)

    def _ar_queue_invoke(self, invocation):
        log.debug("_ar_queue_invoke -> deferred")
        d = deferToThread(invocation[0], *invocation[1], **invocation[2])
        d.addCallback(self._ar_queue_poll)

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
        callTracker = CallTracker(uniqueid, callerid, config)
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
                callTracker.setQueueMember(event['member'])
            except KeyError:
                pass

        log.info("AMI connected")
        ami.registerEvent('Hangup', amiChannelHangup)
        ami.registerEvent('AgentConnect', amiAgentConnected)

        return ami

log = logging.getLogger('A2R')

if __name__ == "__main__":

    logging.basicConfig()

    log.setLevel(logging.DEBUG)
    #manager.log.setLevel(logging.DEBUG)
    #fastagi.log.setLevel(logging.DEBUG)

    # Read configuration from /etc if available
    config = RawConfigParser()
    config.read('/etc/astminer.conf')

    # Read configuration files given on command line
    for f in sys.argv[1:]:
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
    reactor.listenTCP(
            int(config.get('Astminer', 'AgiPort')),
            f, 50,
            config.get('Astminer', 'AgiHost'))
    reactor.run()

