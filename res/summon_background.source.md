# 召唤结果背景来源

- 参考原图：插件根目录的 `real_outcome.jpg`，保留原文件。
- 制作方式：内置 imagegen 编辑一次，清除十一张旧结果卡片及其附属元素，保留数量、奖励和按钮。
- 成品：`res/summon_background.png`，运行时等比适配为 1920×864。
- 动态卡片：Mooncell [英灵图鉴](https://fgo.wiki/w/英灵图鉴) 与 [礼装图鉴](https://fgo.wiki/w/礼装图鉴) 的成品缩略图，按编号缓存，不由 imagegen 生成。
- 顶部余额、持有数量与底部奖励均为参考图中的固定装饰值；没有账户资产或库存计算。

## 实际生成提示词

```text
Use case: precise-object-edit. Edit target: the attached real FGO Chinese summon result screenshot. Make a clean reusable background plate for a deterministic result renderer. REMOVE ONLY ALL ELEVEN character/craft essence cards in the central area, including all their frames, portrait illustrations, stars, class badges, +15/+90 circle bonuses, labels and shadows. Fill these areas seamlessly with the cyan glowing summoning circuitry / blue-black cosmic backdrop matching their immediate surroundings. Keep the exact wide 1920x864 aspect ratio and original composition. Keep ALL other game interface elements unchanged and in original positions: top close button, Saint Quartz 1,110 and 1,173, green icon and 3,849,535, Chinese summon title, inventory counts 387/1000, 973/1000 and 158/1000, bottom 聖晶石消耗奖励/圣晶石消耗奖励 green cube x50 and ALL FOUR blue buttons 编队 强化 继续进行11次召唤 变还. Pixel-faithful UI, keep fonts and numbers, keep screenshot feel. The central region from y180 to y665 must contain ONLY the empty glowing backdrop, absolutely NO cards, portraits, card placeholders, labels, boxes or stars. Do not add content. This is a background asset, not a new mockup.
```
