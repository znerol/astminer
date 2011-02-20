#!/usr/bin/env python

from distutils.core import setup

setup(name='astminer',
    version='0.1',
    description='Asterisk integration for the Redmine issue tracker.',
    author='Lorenz Schori',
    author_email='lo@znerol.ch',
    url='http://github.com/znerol/astminer',
    scripts=['astminer.py'],
    data_files=[
        ('share/doc/astminer/examples', ['astminer.conf']),
        ('share/doc/astminer', ['README', 'LICENSE']),
    ],
)

