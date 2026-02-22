#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Description: 基础配置 - 项目路径、dotenv 加载
"""
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"

load_dotenv(dotenv_path=ENV_PATH)
