from twisted.application.service import ServiceMaker

serviceMaker = ServiceMaker(
    "Astminer",
    "astminer.tap",
    "Asterisk integration for the Redmine issue tracker",
    "astminer",
)
