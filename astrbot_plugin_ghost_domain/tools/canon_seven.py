"""民国七老 · 正典数据。

来源：`神秘复苏_民国七老_资料整理.md`（2026-11-25，带可信度标注）。
接入原则：
- 只采信【明确】条目；【推断】降级为描述性文本，不写死数值；【推测/同人】一律不采。
- 七老不进入主线战斗序列，作为「终局前传」可选副本 + 传承来源。
- 张洞的「抹除」无法用现有战斗表达，改为 NPC 授予传承，不做成可击杀 Boss。
"""

from __future__ import annotations

from typing import Any

from .canon_ghosts import G, I
from .canon_npcs import C
from .canon_world import D, N


def _tag(record: dict[str, Any], level: str, note: str = "") -> dict[str, Any]:
    record["canon"] = level
    if note:
        record["canon_note"] = note
    return record


SEVEN_GHOSTS: list[dict[str, Any]] = [
    _tag(
        G(
            "puppet_master", "木偶人", "S", (320, 30, 16, 8), (0.08, 0.90),
            ("puppet_hands", "木偶拆解", 1.5, 2),
            ("puppet_copy", "木偶人能复制其余六老六成灵异，数量无上限；弱点是前摇偏长。",
             "在前摇完成前打断，可阻止这一次复制。",
             ["木偶的关节声从四面同时响起", "它抬手前会有一次明显的停顿"],
             ["damage_resets_counter"], "puppet_countered"),
            ("brute", {"target_strategy": "weakest", "attack_multiplier": 1.1}),
            "张幼红的鬼：完整灵异木偶人拼图，可逐块取走身体把人拆成积木。",
        ),
        "明确", "能力段可采；「张洞之妹」等同人设定已剔除。",
    ),
    _tag(
        G(
            "grave_field", "无尽坟场", "S", (360, 28, 20, 3), (0.08, 0.90),
            ("grave_bury", "坟土埋葬", 1.4, 2),
            ("bury_live", "坟土自地下涌出，埋葬被入侵的一切，被埋之物无法重启。",
             "离开坟场范围；或用黄金隔绝坟土。",
             ["脚下的地面开始松软", "鞋底沾上了不属于这里的湿土"],
             ["damage_resets_counter"], "grave_countered"),
            ("brute", {"target_strategy": "weakest"}),
            "罗千的坟场：范围最大的镇压型灵异，专克复活流。",
        ),
        "明确", "「具体鬼名」原著未明确，此处以现象命名。",
    ),
    _tag(
        G(
            "meng_clone", "孟小董分身", "S", (280, 26, 14, 9), (0.10, 0.85),
            ("clone_erase", "抹除", 1.45, 1),
            ("infinite_invade", "过去的每个时间点都有她，可从过去入侵现在；每抹除一人就多一个分身。",
             "在被替代前离开 301 的范围，或用高层鬼域压制。",
             ["门外站着和你一模一样的人", "镜子里的人比你先动"],
             ["damage_resets_counter"], "clone_countered"),
            ("brute", {"target_strategy": "random"}),
            "孟小董的无限入侵：七老中最难杀、正面最弱的一位。",
        ),
        "明确", "分身层数「九层/十层」两说并存，未写入数值。",
    ),
]

SEVEN_ITEMS: list[dict[str, Any]] = [
    _tag(I("lacquer_box", "金漆木盒", "罗文松死后留在尸体口袋，用于定位鬼邮局。",
           ["player"], [{"type": "apply_status", "status": "locate", "turns": 1}]), "明确"),
    _tag(I("ghost_shovel", "鬼铁锹", "罗千的器物，锹头粘着永远干不了的泥土。",
           ["ghost"], [{"type": "damage", "amount": 20}]), "明确"),
    _tag(I("black_coffin", "黑棺", "压制力强于红棺，可阻止复苏；张伯华以它自葬。",
           ["player", "controlled_ghost"],
           [{"type": "apply_status", "status": "sealed", "turns": 3}]), "明确"),
    _tag(I("medicine_box", "药箱", "产出压制复苏的中药，无使用风险但有保质期；药效一过反噬更凶（反噬数值待补充）。",
           ["player"], [{"type": "heal", "amount": 20}]), "明确"),
    _tag(I("cheongsam", "旗袍", "张幼红的衣物，可阻挡灵异力量的袭击。",
           ["player", "controlled_ghost"],
           [{"type": "apply_status", "status": "immune", "turns": 2}]), "明确"),
    _tag(I("red_heels", "红色高跟鞋", "免疫以脚印为媒介的灵异，并留下脚印反复召唤尸体。",
           ["player"], [{"type": "apply_status", "status": "footprint_immune", "turns": 2}]), "明确"),
    _tag(I("copper_coin", "铜钱", "引鬼、干扰；连船长也难以避免，仅复苏的张洞可无视。",
           ["ghost"], [{"type": "apply_status", "status": "interfere", "turns": 1}]), "明确"),
    _tag(I("puppet_doll", "人偶娃娃", "与它对视即被盯上；会把攻击转移并与袭击者相互对抗。",
           ["ghost"], [{"type": "apply_status", "status": "redirect", "turns": 2}]), "明确"),
]

SEVEN_DOMAINS: list[dict[str, Any]] = [
    D(
        "faction_seven_elders", "七老祠堂·终局前传", 97,
        "七个人，七种把灵异关回去的办法。他们没能根治，只把债推给了后人。",
        [
            N("elder_hall", "牌位厅", ["入口"], "七块牌位，六块有名字，最左一块是空白的。"),
            N("herb_shop", "药铺", ["线索"], "药柜的抽屉全被拉开，只剩最下面一格是锁着的。"),
            N("post_office_ruin", "邮局旧址", ["线索"], "墙上还留着六点熄灯的告示，墨迹是新的。"),
            N("graveyard", "坟场", ["遭遇"], "坟头没有碑，土却是新翻的。"),
            N("manor_shadow", "古宅影", ["遭遇"], "诡林的每一棵树都朝着同一个方向弯腰。"),
            N("back_door", "后门", ["撤离"], "后门外是公路，路灯亮着。", "lit", "far"),
        ],
        [
            ("seven_graveyard", "graveyard", [("grave_field", 1), ("meng_clone", 1)]),
            ("seven_manor", "manor_shadow", [("puppet_master", 1), ("grave_field", 1)]),
        ],
        "puppet_copy",
        optional=True,
        kit=("holy_water", "ghost_candle", "gold_box", "copper_coin", "medicine_box"),
    ),
]

SEVEN_CHARACTERS: list[dict[str, Any]] = [
    _tag(
        C("zhang_dong", "张洞", "民国七老之首，古宅老人；能力是「抹除」，让鬼直接沉睡。",
          "faction_seven_elders", "manor_shadow",
          "老人坐在红棺边，没有抬头：“你们这一代，总想赢。我们当年只想让它停下来。”",
          [
              ("zd_erase", "请教「抹除」", "他抬手，指向你身后——那里原本什么都没有，现在也什么都没有。",
               [{"type": "unlock_ability", "ability_ref": "legacy_erase"},
                {"type": "san_change", "amount": -10}]),
              ("zd_price", "问代价", "“抹掉的东西，不会回来。包括你自己的一部分。”",
               [{"type": "set_story_flag", "flag": "zhang_dong_met", "value": True},
                {"type": "san_change", "amount": 10}]),
          ]),
        "明确", "「规律鬼」「万法不侵」属同人设定，未采。",
    ),
    _tag(
        C("luo_wensong", "罗文松", "敲门鬼的本体，鬼邮局创建者与首任管理者；门系必死规律可无限叠加。",
          "faction_seven_elders", "post_office_ruin",
          "老人把一封信放在柜台上：“邮局不是邮局，是筛子。你们都是筛下来的。”",
          [
              ("lws_gate", "请教门系规律", "“敲门、开门、熄灯。三件事，做到一件就够了。”",
               [{"type": "unlock_ability", "ability_ref": "legacy_gate"},
                {"type": "reveal_rule", "rule_ref": "three_knocks"}]),
              ("lws_letter", "收下那封信", "信封上没有邮戳，收件人一栏是你的名字。",
               [{"type": "item_add", "item_ref": "lacquer_box", "quantity": 1},
                {"type": "san_change", "amount": -5}]),
          ]),
        "明确", "「关灯鬼/堵门鬼」属推断，未单列。",
    ),
    _tag(
        C("luo_qian", "罗千", "坟场主；埋葬现世，被埋之物无法重启。留下媒介，可被招魂出手一次。",
          "faction_seven_elders", "graveyard",
          "坟场主把铁锹插进土里：“埋下去的东西，就别再挖出来。”",
          [
              ("lq_bury", "请教埋葬", "“坟土不挑对象。人、鬼、时代，都能埋。”",
               [{"type": "unlock_ability", "ability_ref": "legacy_bury"},
                {"type": "item_add", "item_ref": "ghost_shovel", "quantity": 1}]),
              ("lq_medium", "要一份媒介", "他从土里摸出一小袋坟土，递给你。",
               [{"type": "item_add", "item_ref": "grave_soil", "quantity": 2},
                {"type": "set_story_flag", "flag": "luo_qian_medium", "value": True}]),
          ]),
        "明确", "「坟土类鬼名」原著未明确。",
    ),
    _tag(
        C("li_qingzhi", "李庆之", "凯撒酒店的高大男尸；完整鬼影与柴刀的原主，以媒介隔空肢解。",
          "faction_seven_elders", "elder_hall",
          "男尸站在牌位厅角落，尸水在地面积成一小滩：“刀给你，命也给你。想清楚。”",
          [
              ("lqz_cut", "接过柴刀", "刀柄是湿的。你握住它时，听见很远的地方有东西被切开。",
               [{"type": "unlock_ability", "ability_ref": "legacy_cut"},
                {"type": "item_add", "item_ref": "chopper", "quantity": 1}]),
              ("lqz_shadow", "问鬼影的去向", "“头在大川，身子在商场。剩下的，看你运气。”",
               [{"type": "reveal_rule", "rule_ref": "shadow_takeover"},
                {"type": "san_change", "amount": -5}]),
          ]),
        "明确", "",
    ),
    _tag(
        C("meng_xiaodong", "孟小董", "301 室提菜篮的老婆婆；无限入侵，替死娃娃的制作者。",
          "faction_seven_elders", "herb_shop",
          "老婆婆把菜篮放在柜台上：“你要几个我？一个不够，就多来几个。”",
          [
              ("mxd_replace", "请教「替换」", "她眨了眨眼，你发现自己站在了她的位置。",
               [{"type": "unlock_ability", "ability_ref": "legacy_replace"},
                {"type": "san_change", "amount": -10}]),
              ("mxd_doll", "讨一个替死娃娃", "“用完就没了。别省，也别乱用。”",
               [{"type": "item_add", "item_ref": "substitute_doll", "quantity": 2}])
          ]),
        "明确", "「以照片、遗物为媒介」属同人，未采。",
    ),
    _tag(
        C("zhang_youhong", "张幼红（红姐）", "七老之一，死得最早；借柳青青之身苏醒，木偶人可复制六老六成灵异。",
          "faction_seven_elders", "post_office_ruin",
          "红姐坐在邮局台阶上补妆：“红姐。既不是张幼红，也不是柳青青。”",
          [
              ("zyh_copy", "请教「复制」", "她打了个响指，身后站出六个模糊的人影。",
               [{"type": "unlock_ability", "ability_ref": "legacy_copy"},
                {"type": "item_add", "item_ref": "cheongsam", "quantity": 1}]),
              ("zyh_price", "问雇佣的价码", "“二十块鬼钱，一个国王。你有吗？”",
               [{"type": "set_story_flag", "flag": "hong_jie_hired", "value": True},
                {"type": "san_change", "amount": 5}])
          ]),
        "明确", "「张洞之妹」属同人污染，已剔除；「九层/十层」两说未采。",
    ),
    _tag(
        C("zhang_bohua_elder", "张伯华", "七老中最后去世的一位；药铺老人，药箱可强行压住复苏，药效过后反噬更凶。",
          "faction_seven_elders", "herb_shop",
          "老人正在碾药：“药铺要是关了，民国这一代就算彻底过去了。”",
          [
              ("zbh_suppress", "请教「压制」", "他包了一包药：“压得住，但压不久。”",
               [{"type": "unlock_ability", "ability_ref": "legacy_suppress"},
                {"type": "item_add", "item_ref": "medicine_box", "quantity": 1}]),
              ("zbh_fourth", "问第四种存在", "“人驾驭鬼，早晚被鬼吃掉。那就让鬼来当人。”",
               [{"type": "set_story_flag", "flag": "fourth_way", "value": True},
                {"type": "san_change", "amount": 10}])
          ]),
        "明确", "「鬼脑」「大鸿中药铺」属衍生设定，未采。",
    ),
]


# 七老副本需要 3 条主线线索才能进入；副本内每位老还有各自线索。
SEVEN_DOMAINS[0]["requires_clues"] = 3
SEVEN_DOMAINS[0]["min_party_actions"] = 3

_LEGACY_CLUE = {
    "zhang_dong": "clue_zhang_dong",
    "luo_wensong": "clue_luo_wensong",
    "luo_qian": "clue_luo_qian",
    "li_qingzhi": "clue_li_qingzhi",
    "meng_xiaodong": "clue_meng_xiaodong",
    "zhang_youhong": "clue_zhang_youhong",
    "zhang_bohua_elder": "clue_zhang_bohua",
}
for _character in SEVEN_CHARACTERS:
    _flag = _LEGACY_CLUE.get(_character["id"])
    if not _flag:
        continue
    _cid, _label, _text, _effects = _character["choices"][0]
    _character["choices"][0] = (
        _cid,
        _label,
        _text,
        _effects,
        [{"type": "story_flag_matches", "flag": _flag, "value": True}],
    )


def _clue(sid: str, name: str, location: str, text: str, flag: str) -> dict[str, Any]:
    return {
        "id": sid,
        "name": name,
        "description": text,
        "domain": "faction_seven_elders",
        "nodes": [
            (
                f"{sid}_node",
                location,
                text,
                [
                    {"type": "set_story_flag", "flag": flag, "value": True},
                    {"type": "increment_counter", "counter": "clues_found", "amount": 1},
                    {"type": "san_change", "amount": -5},
                ],
                [],
            )
        ],
    }


SEVEN_STORIES: list[dict[str, Any]] = [
    _clue("seven_clue_dong", "红棺旁的名字", "manor_shadow",
          "红棺内侧刻着一个名字：张洞。刻痕很深，像是自己刻的。", "clue_zhang_dong"),
    _clue("seven_clue_wensong", "首任管理者名册", "post_office_ruin",
          "邮局旧址的墙上钉着一块木牌，首任管理者一栏写着：罗文松。", "clue_luo_wensong"),
    _clue("seven_clue_qian", "坟场的铁锹", "graveyard",
          "坟场中央插着一把铁锹，锹柄上刻着「罗」字，泥土永远是湿的。", "clue_luo_qian"),
    _clue("seven_clue_qingzhi", "牌位后的柴刀", "elder_hall",
          "最右侧牌位后面藏着一把锈柴刀，刀背刻着「李庆之」。", "clue_li_qingzhi"),
    _clue("seven_clue_meng", "菜篮与画像", "herb_shop",
          "药铺柜台下有一只旧菜篮，篮底压着一张年轻男人的画像——眉眼像张洞。", "clue_meng_xiaodong"),
    _clue("seven_clue_youhong", "木偶的针脚", "post_office_ruin",
          "一只半成品木偶躺在邮袋里，衣角绣着「红」字，针脚是新的。", "clue_zhang_youhong"),
    _clue("seven_clue_bohua", "药方上的落款", "herb_shop",
          "药柜最下层压着一叠药方，落款全是「张伯华」，最晚一张写着「压不住了」。", "clue_zhang_bohua"),
]
