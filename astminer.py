#!/usr/bin/env python
from twisted.internet import reactor
from starpy import manager, fastagi
from pyactiveresource.activeresource import ActiveResource
import logging, time

log = logging.getLogger('DUR')


class Issue(ActiveResource):
    _site = 'http://rails-lucid.vir:3000'
    _password = "admin"
    _user = "admin"

class CallTracker(object):

    def __init__(self, uniqueid, callerid):
        self.callerid = callerid
        self.uniqueid = uniqueid
        self.startTime = None
        self.talkStart = None
        self.stopTime = None
        self.agent = None
        self.issue = None

    def start(self):
        self.startTime = time.time()
        log.info("%s: start" % self)

        # open ticket here
        self.blockingCreateIssue()

    def setMember(self, member):
        self.talkStart = time.time()
        log.info("%s: member %s answered" % (self, member))
        self.member = member

        # map and assign ticket to member
        self.blockingAssignIssue()

    def hangup(self):
        self.endTime = time.time()
        log.info("%s: stop, total duration: %f" % (self, self.endTime - self.startTime))
        if (self.talkStart != None):
            log.info("%s: stop, talk duration: %f" % (self, self.endTime - self.talkStart))
        else:
            log.info("%s: nobody answered this call", self)

        # update ticket status here
        self.blockingUpdateHangupIssue()

    def blockingCreateIssue(self):
        self.issue = Issue({
            'subject': "Call from %s" % self.callerid,
            'project_id': 1,
            'tracker_id': 1,
            'status_id': 1,
            'priority_id': 4,
            })
        self.issue.save()

    def blockingAssignIssue(self):
        self.issue.assigned_to_id = 3
        self.issue.status_id = 2
        self.issue.save()

    def blockingUpdateHangupIssue(self):
        if self.talkStart != None:
            self.issue.notes = "Hangup. Talked for %f seconds" % (self.endTime - self.talkStart)
        else:
            self.issue.notes = "Hangup. Call not answered"
        self.issue.save()

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
                callTracker.setMember(event['member'])
            except KeyError:
                pass

        log.info("AMI connected")
        ami.registerEvent('Hangup', amiChannelHangup)
        ami.registerEvent('AgentConnect', amiAgentConnected)

        return ami

APPLICATION = Application()

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

