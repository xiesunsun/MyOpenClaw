---
name: image-generator
description: Use when users need expert prompt design for glamour, fashion, editorial, nightlife, swimwear, or portrait-style images of adult women, especially when strong structured prompt analysis will improve the final image prompt.
---

# Image Generator


## 触发范围

- 用户要生成成年女性写真、人像、时尚大片、泳装度假、夜店霓虹、棚拍、古风、二次元真人化
- 用户只给了一个模糊主题，需要先展开成完整摄影 prompt
- 用户希望画面更性感、更吸睛、更有杂志感或商业感
- 用户需要结构化分析、few-shot 风格引导、负面提示词

## 默认边界

- 默认主体是成年女性；如果年龄不明确，主动写成 adult woman、young adult woman、成年女性
- 默认允许更强的性感表达，例如夜店风、泳装风、贴身服装、身体曲线强调
- 保持“写真 / 时尚 / editorial / glamour photography”语境，不把提示词写成露骨色情描述


## 执行顺序

1. 先识别用户要的风格、性感强度、场景、服装、镜头感
2. 用下面的分析框架做结构化拆解
3. 参考 few-shot 示例组织思路
4. 产出三段结果：
   - `Analysis`
   - `Prompt`
   - `Negative Prompt`
5. 只有在用户明确要落地图像文件时，才调用脚本生成图片
6. 生成图片时直接使用脚本绝对路径，不要改写成相对路径

## 分析框架

每次先按这些维度分析，再合成最终 prompt：

- `style_family`
  日系胶片、韩系棚拍、时尚 editorial、夜店霓虹、泳装度假、古风、二次元真人化
- `subject_identity`
  成年女性的人设、气质、职业或人物关键词
- `appearance`
  发型、发色、妆容、肤感、五官风格、身材气质
- `wardrobe`
  服装类型、材质、贴身程度、配饰、高跟鞋/长靴/珠宝等强调点
- `setting`
  酒吧、泳池、海边、酒店套房、摄影棚、都市街景、复古室内等
- `lighting`
  柔光棚拍、逆光金色日落、霓虹侧光、窗边自然光、硬光闪光灯
- `camera_language`
  焦段、机位、景别、镜头压缩感、景深、抓拍或摆拍感
- `pose_and_expression`
  站姿、坐姿、回头、倚靠、撩发、交叉腿、眼神、表情张力
- `sensuality_level`
  低 / 中 / 高。默认取中高，但保持非露骨
- `texture_and_finish`
  胶片颗粒、皮肤质感、商业修图、高清杂志封面感、电影级色彩
- `negative_controls`
  低质量、解剖错误、廉价服装质感、脸崩、手指异常、过度磨皮、脏乱背景

## Few-Shot

用这些示例引导自己的思考方式，不要机械复制字面内容。

### 示例 1: 日系胶片街头写真

**User intent**
想要一个带轻性感和松弛感的日系胶片写真，成年女性，夏夜街头。

**Analysis**
- `style_family`: 日系胶片生活方式写真
- `subject_identity`: 20+ adult woman, soft but confident
- `appearance`: 自然长发，轻透妆容，干净皮肤质感
- `wardrobe`: 短款针织上衣，牛仔短裙，薄外套，细项链
- `setting`: 夏夜便利店外，潮湿街道，城市灯牌
- `lighting`: 便利店顶灯加街头暖色光源，轻微高光溢出
- `camera_language`: 35mm，半身到全身切换，抓拍感，浅景深
- `pose_and_expression`: 回头微笑，单手整理头发，轻松站姿
- `sensuality_level`: 中
- `texture_and_finish`: Fuji 胶片颗粒，暖绿偏色，真实皮肤纹理

**Prompt**
an adult woman in a Japanese lifestyle glamour photoshoot on a humid summer night street, soft confident expression, natural long hair, translucent makeup, short knit top, denim mini skirt, light outer layer, delicate necklace, standing outside a convenience store with glowing city signage and wet pavement reflections, candid 35mm photography, half-body and full-body fashion framing, shallow depth of field, subtle sensuality, natural body line, cinematic warm ambient light mixed with storefront glow, authentic skin texture, Fuji film grain, refined editorial retouching, stylish, intimate, premium, high detail

**Negative Prompt**
underage, childlike, explicit nudity, pornographic framing, bad hands, extra fingers, broken anatomy, cheap fast-fashion texture, over-smoothed skin, plastic face, cluttered background, low contrast, blurry eyes

### 示例 2: 韩系棚拍贴身服装写真

**User intent**
想要高级、干净、身材线条明显的韩系棚拍写真。

**Analysis**
- `style_family`: 韩系商业棚拍
- `subject_identity`: poised adult woman, elegant and self-assured
- `appearance`: 低马尾，精致韩妆，清晰轮廓
- `wardrobe`: bodycon dress, sheer outer layer, high heels, minimalist jewelry
- `setting`: 极简摄影棚，纯净背景
- `lighting`: 大面积柔光箱，边缘轮廓光，商业平衡曝光
- `camera_language`: 85mm，胸像到三分之二身，杂志封面构图
- `pose_and_expression`: 直视镜头，肩颈延展，轻微转胯
- `sensuality_level`: 中高
- `texture_and_finish`: clean luxury editorial, crisp detail, premium beauty retouching

**Prompt**
an adult woman in a high-end Korean studio glamour editorial, poised and self-assured, sleek low ponytail, polished Korean makeup, bodycon dress with a sheer outer layer, elegant high heels, minimalist jewelry, clean seamless studio background, softbox key light with refined rim lighting, 85mm fashion portrait, magazine-cover composition, extended neckline, subtle hip twist, strong feminine silhouette, sophisticated sensual energy, luxury commercial finish, crisp facial detail, premium skin retouching, sleek, modern, upscale

**Negative Prompt**
underage, school uniform, explicit nudity, vulgar styling, low-end clubwear, harsh skin smoothing, warped limbs, asymmetrical eyes, messy backdrop, muddy colors, low-resolution skin detail

### 示例 3: 夜店霓虹风高性感写真

**User intent**
要都市夜店风，性感更强，吸睛，像音乐视频海报。

**Analysis**
- `style_family`: nightclub neon glamour
- `subject_identity`: bold adult woman, dominant nightlife presence
- `appearance`: 波浪长发，浓一点的眼妆，高光唇妆
- `wardrobe`: fitted mini dress, glossy fabric, statement earrings, high boots
- `setting`: upscale club interior, neon signage, reflective bar surfaces
- `lighting`: magenta and cyan rim light, hard specular highlights, dark ambient contrast
- `camera_language`: 50mm，低机位，海报式中心构图
- `pose_and_expression`: 倚靠吧台，直视镜头，自信挑衅感
- `sensuality_level`: 高
- `texture_and_finish`: music-video poster polish, glossy contrast, dramatic cinematic color

**Prompt**
an adult woman in a bold nightclub glamour photoshoot, powerful nightlife presence, long wavy hair, dramatic eye makeup, glossy lips, fitted mini dress with reflective fabric, statement earrings, high boots, leaning against an upscale bar inside a neon-lit club, magenta and cyan rim lighting, deep ambient shadows, glossy reflections, low-angle 50mm photography, poster-like composition, confident provocative gaze, strong body contour emphasis, high sensual tension without explicit nudity, cinematic music-video aesthetics, sharp detail, luxurious nightlife mood

**Negative Prompt**
underage, explicit sexual content, lingerie malfunction, cheap lighting, muddy neon, blurry face, distorted limbs, bad hands, overexposed skin, flat pose, low-end background clutter

### 示例 4: 泳池度假风泳装写真

**User intent**
想要海岛酒店泳池边的高级泳装写真，阳光感强。

**Analysis**
- `style_family`: resort swimwear editorial
- `subject_identity`: confident adult woman on luxury vacation
- `appearance`: 湿发或半湿发，健康光泽皮肤，太阳镜
- `wardrobe`: designer swimsuit, silk cover-up, fine jewelry, sandals
- `setting`: luxury resort poolside, white stone, tropical greenery, sea view
- `lighting`: golden hour sunlight, reflective water light patterns
- `camera_language`: 70mm，长腿比例，横幅时尚构图
- `pose_and_expression`: 坐在泳池边缘或缓步行走，放松但有控制感
- `sensuality_level`: 中高
- `texture_and_finish`: glossy resort campaign, rich skin tones, travel magazine finish

**Prompt**
an adult woman in a luxury resort swimwear editorial, confident vacation mood, healthy glowing skin, slightly wet hair, designer swimsuit with a silk cover-up, delicate jewelry, sunglasses, seated by an upscale poolside with white stone architecture, tropical greenery and distant sea view, golden hour sunlight with shimmering water reflections, elegant 70mm fashion photography, long-leg proportions, relaxed but controlled pose, premium travel magazine styling, sensual yet tasteful glamour, rich skin tones, glossy campaign finish, crisp high detail

**Negative Prompt**
underage, explicit nudity, pornographic pose, cheap bikini styling, plastic skin, awkward proportions, broken hands, messy pool background, washed-out sunlight, low-detail fabric

### 示例 5: 高级时尚 editorial 大片

**User intent**
想要杂志大片感，性感但克制，偏奢侈品牌广告。

**Analysis**
- `style_family`: luxury fashion editorial
- `subject_identity`: elegant adult woman, elite fashion aura
- `appearance`: wet-look hair or sculpted updo, sharp cheek highlight
- `wardrobe`: tailored blazer over fitted dress, thigh-high boots, bold jewelry
- `setting`: luxury hotel suite or marble interior
- `lighting`: directional window light plus subtle fill, sculpted shadows
- `camera_language`: 85mm to 105mm, clean fashion crop, editorial sequencing
- `pose_and_expression`: seated with elongated limbs, calm dominant gaze
- `sensuality_level`: 中高
- `texture_and_finish`: luxury ad polish, crisp tailoring texture, rich blacks and highlights

**Prompt**
an adult woman in a luxury fashion editorial, elegant and dominant presence, sculpted makeup, wet-look hair, tailored blazer layered over a fitted dress, thigh-high boots, bold jewelry, inside a refined marble hotel suite, directional window light with subtle fill, sculpted shadows across the body, 85mm high-fashion photography, elongated limbs, calm commanding gaze, restrained but powerful sensuality, premium brand campaign aesthetic, rich blacks, polished highlights, immaculate fabric texture, ultra-detailed editorial finish

**Negative Prompt**
underage, explicit erotic content, sloppy tailoring, low-fashion styling, flat lighting, dead eyes, warped proportions, bad hands, noisy shadows, excessive skin blur

## 合成规则

生成最终 `Prompt` 时遵循这些规则：

- 先写主体与整体风格，再写外观、服装、场景、光线、镜头、姿态、质感
- 始终使用摄影语言，不要只堆叠空泛形容词
- 性感表达要通过服装、姿态、光线、镜头、气质来体现，不要依赖露骨词汇
- 优先写“高级、商业、杂志、电影、广告、editorial、glamour”这类质感词
- 适度强调 body line、feminine silhouette、confident gaze、luxury mood
- 对用户没说清的点，优先补足能显著提升画面的因素：光线、镜头、姿态、材质、背景层次
- 如果用户只说“来一张美女写真”，默认给出中高性感强度、商业写真语言、干净高级的完成版 prompt

## 默认输出格式

```text
Analysis
- style_family: ...
- subject_identity: ...
- appearance: ...
- wardrobe: ...
- setting: ...
- lighting: ...
- camera_language: ...
- pose_and_expression: ...
- sensuality_level: ...
- texture_and_finish: ...
- negative_controls: ...

Prompt
<一整段可直接投喂模型的完整提示词>

Negative Prompt
<一整段负面提示词>
```

## 生成图片

当用户明确要求直接出图时，直接执行：

```bash
uv run python /Users/ssunxie/code/myopenclaw/.agent/skills/image-generator/scripts/generate_image.py \
  --prompt "<prompt>" \
  --output "<output_path>"
```

仅在用户明确指定时再传 `--aspect-ratio`、`--image-size`、`--model`。默认值：

- model: `gemini-3.1-flash-image-preview`
- aspect ratio: `16:9`
- image size: `1K`

## 失败处理

- 缺少 `GEMINI_API_KEY` 时，直接说明环境变量未配置
- 缺少 `google-genai` 时，直接说明项目依赖未安装
- 模型未返回图像时，把错误和可用文本返回给用户

## 参考

- 官方文档：[Gemini image generation](https://ai.google.dev/gemini-api/docs/image-generation)
