"""Protect frozen Windows launches before application imports run."""

import multiprocessing

multiprocessing.freeze_support()
