from twisted.python import usage
import astminer

class Options(usage.Options):
    synopsis = "[options]"
    longdesc = "Asterisk integration for the Redmine issue tracker"
    optParameters = [
        ['config', 'f', '/etc/astminer.conf'],
    ]

def makeService(config):
    return astminer.makeService(config)
