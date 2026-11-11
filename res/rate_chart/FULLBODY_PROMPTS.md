# 全身表情衍生素材制作记录

方式：内置 imagegen 编辑，透明背景，每种表情单独生成。原始立绘和差分表均保留。

输入 1（编辑目标）：`riyo_gudako.png`。
输入 2（表情参考）：`gudako_expressions.png`。

输出（当前目录）：

- `gudako_laugh_fullbody.png`
- `gudako_cry_fullbody.png`
- `gudako_shocked_fullbody.png`
- `gudako_rage_fullbody.png`

## 完整提示词模板

将 `{expression}` 替换为下文对应的原文，每个输出独立调用一次。

```text
Use case: precise-object-edit. Edit target: image 1, the COMPLETE full-body orange-haired female FGO protagonist sprite. Image 2 is only a facial-expression reference. Produce ONE full-body sprite, centered alone, genuinely transparent background. Change ONLY the face expression to: {expression} Preserve image 1's entire existing body, original white Chaldea uniform with dark stripes, dark skirt, black legwear and shoes, arms-down standing pose, huge-head tiny-body Riyo chibi proportions, orange hairstyle, yellow hairband, thick dark brown outline and flat original colors. Keep ALL hair including top ahoge and ponytail and both feet fully in frame, with small transparent margins. The head must be naturally connected to the neck and the original fully clothed body. Do not use image 2's cropped head framing or exposed-midriff outfit. No extra character, no portrait, no circular badge, no square panel, no text, no background, no shadow.
```

## expression 原文

- laugh: `happy laughing, a wide open laughing mouth like the bottom-left expression in image 2. Keep the same big round golden eyes.`
- cry: `crying, two large tears rolling from her eyes, slightly open small mouth, exactly like the third expression on the first row of small head portraits in image 2.`
- shocked: `shocked and outraged, a very wide open shouting mouth, yellow eyes looking alarmed and vertical stress lines on her forehead, like the second expression on the first row of small head portraits in image 2.`
- rage: `comically furious, huge open jagged triangular-toothed mouth, asymmetrically wide yellow eyes and dark purple forehead stress shading, like the last portrait at the bottom of image 2.`

这些文件为 AI 编辑衍生图，不是官方全身差分。运行时仅使用本地 PNG，不调用图片生成服务。
