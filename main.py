#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Точка входа для хостингов (Bothost и др.)"""
import os
import sys
import importlib.util

current_dir = os.path.dirname(os.path.abspath(__file__))
module_path = os.path.join(current_dir, "3jly.py")

spec = importlib.util.spec_from_file_location("bot_module", module_path)
bot_module = importlib.util.module_from_spec(spec)
sys.modules["bot_module"] = bot_module
spec.loader.exec_module(bot_module)

if __name__ == "__main__":
    bot_module.main()
