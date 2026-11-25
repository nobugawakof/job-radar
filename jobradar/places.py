"""A small built-in gazetteer: countries, major cities and regional groupings.

Used by the region filter (:mod:`jobradar.geo`) to compare *sets of countries*
instead of raw words, so that "Germany" (what you typed) matches a job that
says "EU only", "Berlin" or "EMEA", and "Malaysia" matches "APAC", "Southeast
Asia" or "Kuala Lumpur" — in English and Chinese.

Countries are ISO 3166 alpha-2 codes. Short all-caps abbreviations ("US",
"UK", "EU") only match in capitals, so the pronoun "us" is never a country.
Names that are also common words or first names (Jordan, Chad, Georgia, Austin, Sofia, Milan…) are
left out on purpose: a false place is worse than a missing one, because a job
with any stated place is judged by it.

This is data, not logic (SRS NFR-14): add an alias here to teach the filter a
new name.
"""

from __future__ import annotations

import re
from functools import lru_cache

# code: aliases (English names, cities, Chinese names). Codes themselves are
# not aliases — "DE" or "IN" in a post are far too ambiguous.
COUNTRIES: dict[str, list[str]] = {
    "US": ["United States", "United States of America", "USA", "U.S.A.", "U.S.", "US", "America",
           "New York", "NYC", "San Francisco", "Bay Area", "Silicon Valley", "Seattle",
           "Boston", "Los Angeles", "Chicago", "Denver", "Miami", "Washington DC", "美国", "纽约", "硅谷"],
    "CA": ["Canada", "Toronto", "Vancouver", "Montreal", "加拿大", "多伦多", "温哥华"],
    "MX": ["Mexico", "Mexico City", "墨西哥"],
    "BR": ["Brazil", "São Paulo", "Sao Paulo", "巴西"],
    "AR": ["Argentina", "Buenos Aires", "阿根廷"],
    "CL": ["Chile", "智利"],
    "CO": ["Colombia", "Bogotá", "Bogota", "Medellín", "Medellin", "哥伦比亚"],
    "PE": ["Peru", "Lima", "秘鲁"],
    "UY": ["Uruguay", "Montevideo"],
    "CR": ["Costa Rica"],
    "GB": ["United Kingdom", "UK", "U.K.", "Great Britain", "Britain", "England", "Scotland", "Wales",
           "London", "Manchester", "Edinburgh", "英国", "伦敦"],
    "IE": ["Ireland", "Dublin", "爱尔兰"],
    "DE": ["Germany", "Deutschland", "Berlin", "Munich", "München", "Hamburg", "Frankfurt", "德国", "柏林"],
    "FR": ["France", "Paris", "Lyon", "法国", "巴黎"],
    "ES": ["Spain", "Madrid", "Barcelona", "西班牙"],
    "PT": ["Portugal", "Lisbon", "Lisboa", "Porto", "葡萄牙"],
    "IT": ["Italy", "Rome", "意大利"],
    "NL": ["Netherlands", "Amsterdam", "Rotterdam", "荷兰", "阿姆斯特丹"],
    "BE": ["Belgium", "Brussels", "比利时"],
    "LU": ["Luxembourg", "卢森堡"],
    "CH": ["Switzerland", "Zurich", "Zürich", "Geneva", "瑞士"],
    "AT": ["Austria", "Vienna", "奥地利"],
    "PL": ["Poland", "Warsaw", "Kraków", "Krakow", "Wrocław", "Wroclaw", "波兰"],
    "CZ": ["Czechia", "Czech Republic", "Prague", "捷克"],
    "SK": ["Slovakia", "Bratislava"],
    "HU": ["Hungary", "Budapest", "匈牙利"],
    "RO": ["Romania", "Bucharest", "罗马尼亚"],
    "BG": ["Bulgaria", "保加利亚"],
    "HR": ["Croatia", "Zagreb"],
    "SI": ["Slovenia", "Ljubljana"],
    "GR": ["Greece", "Athens", "希腊"],
    "CY": ["Cyprus", "塞浦路斯"],
    "MT": ["Malta"],
    "EE": ["Estonia", "Tallinn", "爱沙尼亚"],
    "LV": ["Latvia", "Riga"],
    "LT": ["Lithuania", "Vilnius"],
    "FI": ["Finland", "Helsinki", "芬兰"],
    "SE": ["Sweden", "Stockholm", "瑞典"],
    "DK": ["Denmark", "Copenhagen", "丹麦"],
    "NO": ["Norway", "Oslo", "挪威"],
    "IS": ["Iceland", "Reykjavik", "冰岛"],
    "UA": ["Ukraine", "Kyiv", "Kiev", "乌克兰"],
    "RS": ["Serbia", "Belgrade"],
    "TR": ["Turkey", "Türkiye", "Istanbul", "土耳其"],
    "IL": ["Israel", "Tel Aviv", "以色列"],
    "AE": ["United Arab Emirates", "UAE", "Dubai", "Abu Dhabi", "阿联酋", "迪拜"],
    "SA": ["Saudi Arabia", "KSA", "Riyadh", "沙特"],
    "QA": ["Qatar", "Doha", "卡塔尔"],
    "EG": ["Egypt", "Cairo", "埃及"],
    "NG": ["Nigeria", "Lagos", "尼日利亚"],
    "KE": ["Kenya", "Nairobi", "肯尼亚"],
    "ZA": ["South Africa", "Cape Town", "Johannesburg", "南非"],
    "MA": ["Morocco", "Casablanca", "摩洛哥"],
    "GH": ["Ghana", "Accra"],
    "CN": ["China", "Mainland China", "PRC", "Beijing", "Shanghai", "Shenzhen", "Hangzhou", "Guangzhou",
           "Chengdu", "中国", "中国大陆", "大陆", "国内", "北京", "上海", "深圳", "杭州", "广州", "成都",
           "南京", "武汉", "西安", "苏州", "厦门", "长沙", "天津", "重庆"],
    "HK": ["Hong Kong", "HK", "香港"],
    "MO": ["Macau", "Macao", "澳门"],
    "TW": ["Taiwan", "Taipei", "台湾", "台北"],
    "JP": ["Japan", "Tokyo", "Osaka", "日本", "东京", "東京", "大阪"],
    "KR": ["South Korea", "Korea", "Seoul", "韩国", "首尔"],
    "SG": ["Singapore", "新加坡"],
    "MY": ["Malaysia", "Kuala Lumpur", "Penang", "马来西亚", "吉隆坡"],
    "ID": ["Indonesia", "Jakarta", "Bali", "印尼", "印度尼西亚"],
    "PH": ["Philippines", "Manila", "菲律宾"],
    "VN": ["Vietnam", "Viet Nam", "Hanoi", "Ho Chi Minh", "Saigon", "越南"],
    "TH": ["Thailand", "Bangkok", "泰国", "曼谷"],
    "KH": ["Cambodia", "Phnom Penh", "柬埔寨"],
    "MM": ["Myanmar", "缅甸"],
    "IN": ["India", "Bangalore", "Bengaluru", "Mumbai", "Delhi", "Hyderabad", "Pune", "Chennai", "印度"],
    "PK": ["Pakistan", "Karachi", "Lahore", "巴基斯坦"],
    "BD": ["Bangladesh", "Dhaka", "孟加拉"],
    "LK": ["Sri Lanka", "Colombo"],
    "NP": ["Nepal", "Kathmandu"],
    "AU": ["Australia", "Sydney", "Melbourne", "Brisbane", "Perth", "澳大利亚", "澳洲", "悉尼"],
    "NZ": ["New Zealand", "NZ", "Auckland", "Wellington", "新西兰"],
}

_EU = ["AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT",
       "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"]
_EUROPE = _EU + ["GB", "NO", "CH", "IS", "UA", "RS"]
_MIDDLE_EAST = ["AE", "SA", "QA", "IL", "TR", "EG"]
_AFRICA = ["NG", "KE", "ZA", "EG", "MA", "GH"]
_SEA = ["SG", "MY", "ID", "PH", "VN", "TH", "KH", "MM"]
_GREATER_CHINA = ["CN", "HK", "MO", "TW"]
_ASIA = _SEA + _GREATER_CHINA + ["JP", "KR", "IN", "PK", "BD", "LK", "NP"]
_LATAM = ["MX", "BR", "AR", "CL", "CO", "PE", "UY", "CR"]

# grouping: (aliases, member countries)
GROUPS: dict[str, tuple[list[str], list[str]]] = {
    "EU": (["EU", "E.U.", "European Union", "欧盟"], _EU),
    "EEA": (["EEA", "European Economic Area"], _EU + ["NO", "IS"]),
    "Europe": (["Europe", "European", "欧洲"], _EUROPE),
    "EMEA": (["EMEA"], _EUROPE + _MIDDLE_EAST + _AFRICA),
    "Middle East": (["Middle East", "MENA", "中东"], _MIDDLE_EAST),
    "Africa": (["Africa", "非洲"], _AFRICA),
    "UK & Ireland": (["UK&I", "UKI"], ["GB", "IE"]),
    "DACH": (["DACH"], ["DE", "AT", "CH"]),
    "Nordics": (["Nordics", "Nordic", "Scandinavia", "北欧"], ["SE", "NO", "DK", "FI", "IS"]),
    "Benelux": (["Benelux"], ["BE", "NL", "LU"]),
    "CEE": (["CEE", "Eastern Europe", "Central Europe", "东欧"],
            ["PL", "CZ", "SK", "HU", "RO", "BG", "HR", "SI", "EE", "LV", "LT", "UA", "RS"]),
    "North America": (["North America", "北美"], ["US", "CA", "MX"]),
    "LATAM": (["LATAM", "LatAm", "Latin America", "South America", "Central America", "拉美", "南美"], _LATAM),
    "Americas": (["Americas", "美洲"], ["US", "CA"] + _LATAM),
    "APAC": (["APAC", "Asia Pacific", "Asia-Pacific", "亚太"], _ASIA + ["AU", "NZ"]),
    "Asia": (["Asia", "亚洲"], _ASIA),
    "Southeast Asia": (["Southeast Asia", "South East Asia", "South-East Asia", "SEA", "ASEAN", "东南亚"], _SEA),
    "Greater China": (["Greater China", "大中华", "港澳台"], _GREATER_CHINA),
    "ANZ": (["ANZ", "Oceania", "大洋洲"], ["AU", "NZ"]),
}


# "Austin, TX" / "Toronto, ON": a capitalised place followed by a US state or
# Canadian province code. Far safer than bare city names (a person's name is
# never followed by ", TX").
# OR, IN, ME, OK, HI and ID are left out: in capitals they're everyday words
# ("REMOTE, OR HYBRID").
_US_STATES = ("AL AK AZ AR CA CO CT DE FL GA IL IA KS KY LA MD MA MI MN MS MO MT NE NV NH "
              "NJ NM NY NC ND OH PA RI SC SD TN TX UT VT VA WA WV WI WY DC").split()
_CA_PROVINCES = "ON BC QC AB MB SK NS NB NL PE".split()
_CITY_STATE = re.compile(r"\b[A-Z][A-Za-z.\-]+(?:\s[A-Z][A-Za-z.\-]+)?,\s?(" +
                         "|".join(_US_STATES + _CA_PROVINCES) + r")\b(?![A-Za-z])")


def _is_cjk(s: str) -> bool:
    return any("　" <= c <= "鿿" for c in s)


@lru_cache(maxsize=1)
def _index():
    """(alias → codes) and three compiled matchers: case-sensitive
    abbreviations, case-insensitive names, CJK substrings."""
    table: dict[str, set[str]] = {}

    def add(alias: str, codes) -> None:
        key = alias if (alias.replace(".", "").replace("&", "").isupper() and len(alias.replace(".", "")) <= 4) \
            else alias.lower()
        table.setdefault(key, set()).update(codes)

    for code, aliases in COUNTRIES.items():
        for a in aliases:
            add(a, [code])
    for aliases, members in GROUPS.values():
        for a in aliases:
            add(a, members)

    def alt(keys):
        return "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True))

    caps = [k for k in table if not _is_cjk(k) and k != k.lower()]
    words = [k for k in table if not _is_cjk(k) and k == k.lower()]
    cjk = [k for k in table if _is_cjk(k)]
    bound = (r"(?<![A-Za-z0-9])(?:{})(?![A-Za-z0-9])")
    return (table,
            re.compile(bound.format(alt(caps))) if caps else None,
            re.compile(bound.format(alt(words)), re.I) if words else None,
            re.compile(alt(cjk)) if cjk else None,
            re.compile(bound.format(alt(caps)), re.I) if caps else None)


def find(text: str | None, *, any_case: bool = False) -> set[str]:
    """Countries a piece of text names, directly or through a grouping.

    Abbreviations ("US", "UK", "EU") only count in capitals, so "contact us"
    isn't a country — unless ``any_case``, for text already known to be about
    geography ("anywhere in the us", "uk only").
    """
    if not text:
        return set()
    table, caps, words, cjk, caps_any = _index()
    upper = {k.upper(): k for k in table if k != k.lower() and not _is_cjk(k)}
    found: set[str] = set()
    for m in (caps_any if any_case else caps).finditer(text) if caps else ():
        found |= table.get(upper.get(m.group(0).upper(), m.group(0)), set())
    if words:
        for m in words.finditer(text):
            found |= table.get(m.group(0).lower(), set())
    if cjk:
        for m in cjk.finditer(text):
            found |= table.get(m.group(0), set())
    for m in _CITY_STATE.finditer(text):
        found.add("CA" if m.group(1) in _CA_PROVINCES else "US")
    return found


def resolve(term: str) -> set[str]:
    """Countries for one region the user typed ("Germany", "EU", "马来西亚"),
    or an empty set if it isn't a known place (then it's matched as text)."""
    t = term.strip()
    if not t:
        return set()
    table = _index()[0]
    for key in (t, t.lower(), t.upper()):
        if key in table:
            return set(table[key])
    return find(t) if len(t) > 3 else set()
