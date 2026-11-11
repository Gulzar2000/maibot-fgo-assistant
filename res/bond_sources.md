# 羁绊挖矿数据与口径

- 功能参考：[okita27/FGO-bond-quartz](https://okita27.github.io/FGO-bond-quartz/)。独立实现计算和 Pillow 绘图，没有复制其 HTML、CSS 或 Canvas 实现。
- 国服从者、图鉴号、名称、星级与累计羁绊经验：[Atlas Academy nice_servant](https://api.atlasacademy.io/export/CN/nice_servant.json)。礼装排除索引：[basic_equip](https://api.atlasacademy.io/export/CN/basic_equip.json)。仅保留 normal 从者，heroine（玛修）及其他类型不计奖励；相同图鉴号共享形态进度必须一致。
- 奖励核对：[Mooncell 圣晶石](https://fgo.wiki/w/圣晶石)、[安哥拉曼纽的牵绊点数](https://fgo.wiki/w/安哥拉曼纽#牵绊点数)。2026-11-10 核对零星奖励为 6～9 绊各 2 石，与参考网页默认按一星处理不同。
- 五星在 6～9 绊各 3 石，四星各 2 石，三星各 1 石，二星在 8、9 绊各 1 石，一星在 9 绊 1 石；11～15 绊各 30 石，10 绊无圣晶石奖励。只计算羁绊等级直接奖励，不包含任务、幕间或其他奖励。
- 国服 nice 数据中的 bondGifts 为空，不把空表解释成零奖励；也不采用日服当前奖励替代国服规则。经验使用 CN 累计表，未使用 JP 或 Wiki 的不同版本经验曲线。
- `userSvtCollection.status == 2` 的契约图鉴计入，包括曾获得但已出售的从者。重复卡片不产生第二份羁绊收益；缺少已契约图鉴、字段缺失、共享形态进度冲突或等级异常时停止计算。
- 当前上限为 `10 + friendshipExceedCount`，15 绊封顶；累计经验为 `friendship`，当前等级为 `friendshipRank`。当前等级及上限有效时才计算距下一级经验。
- 梦火库存来自本次完整 `userItem` 表中道具 ID `1000` 的 `num`，缺行代表零库存，缺失整张表则报错。已喂未兑现数量为 `上限 - max(10, 当前等级)`；背包额外潜力按实际库存与现有从者剩余可投入空间的较小值乘以 30，单独展示，不按全员喂满计算。
- 自动对比只使用同一实际游戏角色的上次快照。原有从者跨过的奖励等级合计为升级奖励，新喂梦火增加的可获得奖励另列；新增或缺失从者不推算期间领取。只保存最近一次快照及计算所需字段，不保存原始登录响应。

公共缓存：`fgogacha/bond_metadata_cn.json`；上次角色快照：`fgogacha/bond_history.json`。公共数据到期整体刷新，遇未知图鉴即时刷新一次，失败不隐式回退旧数据。

羁绊每日取数单独记录于 `fgogacha/bond_capture_days.json`，不读写真实出率的 `capture_history.json`；两条指令的待确认状态也独立保存于内存，同账号操作占用状态继续共用。

缓存 schema 2 使用 [国服完整 mstSvt 主表](https://api.atlasacademy.io/repo/CN/master/mstSvt.json) 识别导出从者表未收录的 `collectionNo == 0` 实体并排除。例如 `104001` 是葛饰北斋的宝具演出实体（type 12），不是独立羁绊从者。旧 schema 1 缓存首次查询自动刷新；未收录的正图鉴号仍报错，不猜测奖励。
