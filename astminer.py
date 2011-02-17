#!/usr/bin/env python
from twisted.internet import reactor
from twisted.internet.defer import DeferredQueue
from twisted.internet.threads import deferToThread
from starpy import manager, fastagi
from pyactiveresource.activeresource import ActiveResource
import logging, time

ISSUE_CREATE_TMPL= {
    'subject': "Call from %(callerid)s",
    'project_id': 1,
    'tracker_id': 1,
    'status_id': 1,
    'priority_id': 4,
}

ISSUE_ASSIGN_TMPL= {
    'assigned_to_id': '%(assigned_to_id)d',
    'status_id': 2,
    'notes': 'Queue member %(queueMember)s answered'
}

ISSUE_ASSIGN_USER_NOT_FOUND_TMPL = {
    'notes': 'Redmine user for queue member %(queueMember)s not found'
}

ISSUE_HANGUP_CALL_ANSWERED_TMPL = {
    'notes': 'Hangup. Talked for %(talkDuration)f seconds'
}

ISSUE_HANGUP_CALL_NOT_ANSWERED_TMPL = {
    'notes': 'Hangup. Call not answered'
}

REDMINE_SITE = 'http://rails-lucid.vir/'
REDMINE_USER = 'admin'
REDMINE_PASSWORD = 'admin'

class Issue(ActiveResource):
    _site = REDMINE_SITE
    _user = REDMINE_USER
    _password = REDMINE_PASSWORD

class User(ActiveResource):
    _site = REDMINE_SITE
    _user = REDMINE_USER
    _password = REDMINE_PASSWORD

class CallTracker(object):

    def __init__(self, uniqueid, callerid):
        self.callerid = callerid
        self.uniqueid = uniqueid
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

    def _mergeIssueWithTemplate(self, template):
        for (k,v) in template.iteritems():
            try:
                v = v % self._issue_placeholders
            except TypeError:
                pass

            setattr(self.issue, k, v)

    def _blockingCreateIssue(self):
        log.debug('Enter _blockingCreateIssue')
        self._issue_placeholders['callerid'] = self.callerid
        self._issue_placeholders['uniqueid'] = self.uniqueid
        self._issue_placeholders['startTime'] = self.startTime

        self.issue = Issue({})
        self._mergeIssueWithTemplate(ISSUE_CREATE_TMPL)

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

        if found:
            self._mergeIssueWithTemplate(ISSUE_ASSIGN_TMPL)
        else:
            log.warning('Redmine user for queue member %s not found' % self.queueMember)
            self._mergeIssueWithTemplate(ISSUE_ASSIGN_USER_NOT_FOUND_TMPL)

        self.issue.save()
        log.debug('Exit _blockingAssignIssue')

    def _blockingUpdateHangupIssue(self):
        log.debug('Enter _blockingUpdateHangupIssue')

        self._issue_placeholders['stopTime'] = self.stopTime
        self._issue_placeholders['callDuration'] = self.callDuration
        self._issue_placeholders['talkDuration'] = self.talkDuration
        if self.callAnswered:
            self._mergeIssueWithTemplate(ISSUE_HANGUP_CALL_ANSWERED_TMPL)
        else:
            self._mergeIssueWithTemplate(ISSUE_HANGUP_CALL_NOT_ANSWERED_TMPL)

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

    def __init__(self):
        self.trackers = {}

    def agiRequestReceived(self, agi):
        uniqueid = agi.variables['agi_uniqueid']
        callerid = agi.variables['agi_callerid']
        callTracker = CallTracker(uniqueid, callerid)
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

APPLICATION = Application()
log = logging.getLogger('A2R')

if __name__ == "__main__":
    logging.basicConfig()

    log.setLevel(logging.DEBUG)
    #manager.log.setLevel(logging.DEBUG)
    #fastagi.log.setLevel(logging.DEBUG)

    theManager = manager.AMIFactory("manager", "1234")
    m = theManager.login("handz-pbx.vir", 5038, 10)
    m.addCallback(APPLICATION.amiConnectionMade)

    f = fastagi.FastAGIFactory(APPLICATION.agiRequestReceived)
    reactor.listenTCP(4576, f, 50, "0.0.0.0") 
    reactor.run()

