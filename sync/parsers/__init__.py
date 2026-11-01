# -*- coding: utf-8 -*-
"""四个解析器。统一契约：parse(html) -> {"items": [...], "unparsed": [...]}。"""

from . import tju_courses, tju_exams, zhs_work, zhs_progress

__all__ = ["tju_courses", "tju_exams", "zhs_work", "zhs_progress"]
