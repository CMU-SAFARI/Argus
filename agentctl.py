#!/usr/bin/env python3
"""Repo-root launcher for the agentctl CLI.
Usage: ./agentctl reference stream --n-runs 30
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "orchestrator"))
from agentctl.cli import main
sys.exit(main())
