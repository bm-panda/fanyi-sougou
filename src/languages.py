# -*- coding: utf-8 -*-
"""语言总表 —— 界面上能选的语言、两个引擎各自认什么，全在这里。

为什么不是一张简单的表：两个引擎支持的集合**不重合**，必须分开维护。

    在线（搜狗 /text 接口）  实测只认 21 种。繁体中文 `zh-CHT`、印尼语 `id`、
                            印地语 `hi`、粤语 `yue` 等一律返回原文（= 不支持）。
                            注：社区包 sogou-translate 里那张更大的码表属于
                            **企业版接口**，Web 版不认，别照搬。
    离线（Hy-MT2）          官方 38 种，多出繁体中文 / 粤语 / 马来语 / 印尼语 /
                            印地语等 21 种，但**没有**芬兰语 / 瑞典语 / 丹麦语 / 匈牙利语。

两者只有 17 种重合，所以界面按引擎切换语言列表，而不是取并集。

语言名的三种形态：
    中文名    界面显示 + 盒子 params 传参（用户和调用方都只认这个）
    搜狗码    在线引擎 ——— 在线 prompt 就是一个 URL 参数
    英文名    离线引擎 ——— 官方要求「英文 prompt 配英文语言名」

改动本表前请重跑项目里的语言码实测脚本，别凭印象加语言。
"""

LANG_AUTO = '自动识别'

#: (中文名, 搜狗码 or None, 离线英文名 or None)
LANGUAGES = [
    # ---- 两个引擎都支持 ----
    ('中文',       'zh-CHS', 'Chinese'),
    ('英文',       'en',     'English'),
    ('日语',       'ja',     'Japanese'),
    ('韩语',       'ko',     'Korean'),
    ('法语',       'fr',     'French'),
    ('德语',       'de',     'German'),
    ('俄语',       'ru',     'Russian'),
    ('西班牙语',   'es',     'Spanish'),
    ('葡萄牙语',   'pt',     'Portuguese'),
    ('意大利语',   'it',     'Italian'),
    ('荷兰语',     'nl',     'Dutch'),
    ('波兰语',     'pl',     'Polish'),
    ('捷克语',     'cs',     'Czech'),
    ('土耳其语',   'tr',     'Turkish'),
    ('阿拉伯语',   'ar',     'Arabic'),
    ('泰语',       'th',     'Thai'),
    ('越南语',     'vi',     'Vietnamese'),
    # ---- 仅在线支持 ----
    ('芬兰语',     'fi',     None),
    ('瑞典语',     'sv',     None),
    ('丹麦语',     'da',     None),
    ('匈牙利语',   'hu',     None),
    # ---- 仅离线支持 ----
    ('繁体中文',   None,     'Traditional Chinese'),
    ('粤语',       None,     'Cantonese'),
    ('马来语',     None,     'Malay'),
    ('印尼语',     None,     'Indonesian'),
    ('菲律宾语',   None,     'Filipino'),
    ('印地语',     None,     'Hindi'),
    ('高棉语',     None,     'Khmer'),
    ('缅甸语',     None,     'Burmese'),
    ('波斯语',     None,     'Persian'),
    ('古吉拉特语', None,     'Gujarati'),
    ('乌尔都语',   None,     'Urdu'),
    ('泰卢固语',   None,     'Telugu'),
    ('马拉地语',   None,     'Marathi'),
    ('希伯来语',   None,     'Hebrew'),
    ('孟加拉语',   None,     'Bengali'),
    ('泰米尔语',   None,     'Tamil'),
    ('乌克兰语',   None,     'Ukrainian'),
    ('藏语',       None,     'Tibetan'),
    ('哈萨克语',   None,     'Kazakh'),
    ('蒙古语',     None,     'Mongolian'),
    ('维吾尔语',   None,     'Uyghur'),
]

# 中文名 -> 语言码 / 语言名。两个字典都排除 None 项。
_ONLINE = {name: code for name, code, _ in LANGUAGES if code}
_OFFLINE = {name: en for name, _, en in LANGUAGES if en}

# 「自动识别」只在在线引擎有意义（对应接口的 auto）；离线引擎靠 src=None 表达。
_ONLINE[LANG_AUTO] = 'auto'


def online_targets():
    """在线引擎支持的目标语言（21 种，不含自动识别）。"""
    return [name for name, code, _ in LANGUAGES if code]


def offline_targets():
    """离线引擎支持的目标语言（38 种，不含自动识别）。"""
    return [name for name, _, en in LANGUAGES if en]


def lang_names(engine_key):
    """某引擎下拉框用的完整候选（含「自动识别」）。engine_key 为 'online' / 'offline'。"""
    targets = offline_targets() if engine_key == 'offline' else online_targets()
    return [LANG_AUTO] + targets


def online_code(name):
    """中文名 -> 搜狗语言码；不支持返回 None。「自动识别」-> 'auto'。"""
    return _ONLINE.get(name)


def offline_name(name):
    """中文名 -> 离线 prompt 用的英文语言名；不支持或自动识别返回 None。"""
    return _OFFLINE.get(name)


#: 走**中文 prompt** 的离线目标语言（值是 offline_name 给出的英文名）。
#: 官方规则是「中文 prompt 配中文语言名、英文 prompt 配英文语言名」，两套都得对。
#: 这几个语言在英文模板下实测是坏的 —— 繁体中文会原样吐简体、粤语甚至直接翻成英文，
#: 换成中文模板 + 中文名就正常。其余语言英文模板下实测均正常，不必切。
ZH_PROMPT_TARGETS = frozenset({'Traditional Chinese', 'Cantonese'})

_EN2ZH = {en: zh for zh, _, en in LANGUAGES if en}


def zh_name(en_name):
    """离线英文语言名 -> 中文名（中文模板用）。未知的原样返回。"""
    return _EN2ZH.get(en_name, en_name)
