# B站音源点歌 · bilibili_music

> 给 N.E.K.O 一个能用的 B 站音源：说歌名，自动搜索、提取音频、缓存并播放。

![Platform](https://img.shields.io/badge/platform-Windows-blue)
![N.E.K.O](https://img.shields.io/badge/N.E.K.O-plugin-ff69b4)
![Python](https://img.shields.io/badge/python-3.11-3776AB)

N.E.K.O 自带的点歌台经常因为版权、会员或曲库限制搜不到歌、放不出来。这个插件把音源换成 **B 站公开视频**：用户说“点一首晴天”，插件会搜索 B 站、用 yt-dlp 提取最佳音频、下载到本地，再通过 N.E.K.O 原生播放协议交给播放器。

> 仅用于个人学习与交流。请遵守 B 站用户协议与版权规定，不要用于商业传播或重新分发音频。

---

## 功能

- 🎵 **聊天点歌**：对角色说歌名即可，无需打开额外窗口。
- 🔍 **B 站搜索**：取搜索结果的第一个视频，支持“歌名 歌手”提高准确率。
- 🎧 **最佳音频**：yt-dlp 优先选择纯音频 m4a，跳过 m3u8。
- 💾 **本地缓存**：音频下载到 `static/audio/`，同一首歌再次点播直接复用。
- 🚫 **绕开直链 403**：带 B 站 Referer 下载，不依赖 B 站 CDN 的浏览器直链。
- 📦 **依赖自注入**：不需要给 N.E.K.O 主程序安装 Python 包，依赖装进插件自己的 `_vendor`。
- 🛡️ **友好报错**：搜不到、超时、风控、yt-dlp 解析失败都有中文提示。
- 🍪 **可选 Cookies**：B 站风控严格时，可放入浏览器导出的 cookies。

## 环境要求

| 项目 | 要求 |
| --- | --- |
| 系统 | Windows 10 / 11（当前实测环境） |
| N.E.K.O | Steam / 桌面版，内置 Python 3.11 |
| 安装器 Python | 系统里任意带 pip 的 Python（推荐 3.11，用于把依赖装进插件目录） |
| 网络 | 能访问 B 站与 PyPI |

> 不需要、也不应该往 N.E.K.O 安装目录里直接 `pip install`。

---

## 安装

### 1. 放到用户插件目录

把整个 `bilibili_music` 文件夹放到：

```text
C:\Users\<用户名>\AppData\Local\N.E.K.O\.neko-plugin-installations\plugins\bilibili_music
```

也就是：

```text
%LOCALAPPDATA%\N.E.K.O\.neko-plugin-installations\plugins\bilibili_music
```

⚠️ **不要**放到：

```text
E:\SteamLibrary\steamapps\common\n.e.k.o\resources\bin\plugin\plugins\
```

后者是内置插件目录。N.E.K.O 运行中新增的内置插件不会热启动，会直接报：

```text
start_plugin failed
```

而且插件日志是空的。放到用户插件目录后，刷新/重载即可正常启动。

### 2. 安装依赖

在插件目录打开终端：

```powershell
cd "$env:LOCALAPPDATA\N.E.K.O\.neko-plugin-installations\plugins\bilibili_music"
python bootstrap_deps.py --target-python 3.11
```

脚本会把 `requests`、`yt-dlp` 及其依赖安装到：

```text
bilibili_music\_vendor
```

安装日志：

```text
bilibili_music\_vendor\install.log
```

如果系统里有多个 Python，可以指定：

```powershell
python bootstrap_deps.py --target-python 3.11 --python "C:\Python311\python.exe"
```

也可以不手动跑：第一次点歌时插件会自动安装。只是首次点歌会多等一会儿。

### 3. 启用插件

打开 N.E.K.O 插件管理器，找到 **B站音源点歌**，点击启动/重载。

启动成功的日志里应该能看到：

```text
Plugin entries collected via collect_entries(): ['startup', 'play_song']
```

### 4. 点歌

对角色说：

- “点一首晴天”
- “我想听周杰伦的稻香”
- “播放《花海》”

建议写成 **“歌名 歌手”**，搜索结果会更准。

---

## 为什么不用 N.E.K.O 的 /api/music/proxy

B 站音频 CDN 会校验 `Referer: https://www.bilibili.com/`，而 N.E.K.O 的媒体代理只放行固定域名。实际测试中：

- yt-dlp 返回的音频经常在 `*.mcdn.bilivideo.cn`；
- `bilivideo.cn` 不在代理白名单里，代理会返回 403；
- 浏览器直接播 B 站直链也会因为 Referer 被 403。

所以插件改成：

1. 带 B 站 Referer 下载音频；
2. 存到插件自己的 `static/audio/`；
3. 用本地静态 URL 播放。

本地 URL 形如：

```text
http://127.0.0.1:48916/plugin/bilibili_music/ui/audio/BV1xxxxxxxxx.m4a
```

支持 HTTP Range，播放器可以拖动进度条。

---

## 播放协议

插件发送两条 N.E.K.O 原生消息，和内置 `music_pusher` 插件一致：

### 1. `music_allowlist_add`

```json
{
  "domains": ["127.0.0.1", "localhost", "::1"],
  "http_urls": ["http://127.0.0.1:48916/plugin/bilibili_music/ui/audio/xxx.m4a"],
  "event_id": "bili_music_xxx"
}
```

把本地播放地址加入前端白名单。

### 2. `music_play_url`

```json
{
  "url": "http://127.0.0.1:48916/plugin/bilibili_music/ui/audio/xxx.m4a",
  "name": "晴天",
  "artist": "周杰伦",
  "event_id": "bili_music_xxx"
}
```

前端收到后调用音乐播放器播放本地文件。

---

## 工作流程

```text
用户在聊天里点歌
        │
        ▼
B 站搜索接口（requests + UA/Referer/Cookie）
        │
        ▼
取第一条视频的 bvid
        │
        ▼
yt-dlp 解析 bestaudio（优先 m4a）
        │
        ▼
带 Referer 下载到 static/audio/<bvid>.m4a
        │
        ▼
music_allowlist_add + music_play_url
        │
        ▼
N.E.K.O 前端播放器播放
```

---

## 项目结构

```text
bilibili_music/
├── __init__.py            # 插件本体：搜索、解析、下载、推送
├── bootstrap_deps.py      # 依赖检查 / 安装到 _vendor
├── plugin.toml            # N.E.K.O 插件清单
├── requirements.txt       # 普通 Python 环境的依赖清单
├── README.md
├── static/
│   ├── index.html
│   └── audio/             # 下载缓存（首次点歌后生成）
└── _vendor/               # 第三方依赖（bootstrap 后生成）
```

`_vendor/` 和 `static/audio/` 已在 `.gitignore` 中排除，不会提交到 GitHub。

---

## 冻结应用的依赖注入

N.E.K.O 是 PyInstaller / Nuitka 打包的冻结应用，安装目录里没有独立 `python.exe`，不能直接对主程序 `pip install`。插件这样解决：

1. `__init__.py` 最顶层把插件目录和 `_vendor` 加入 `sys.path`；
   `_vendor` 放在最前面，同名库优先用插件自带的版本。
2. 插件加载阶段只做 `importlib.util.find_spec` 轻量检查，不真正 import，
   也不安装，避免拖过宿主约 1 秒的启动就绪时限。
3. 第一次点歌时，`bootstrap_deps.py` 找一个系统 Python 的 pip，
   把依赖装到 `_vendor`。
4. 安装时使用：

```text
--target _vendor
--only-binary=:all:
--python-version 3.11
--platform win_amd64
--abi cp311
--no-compile
```

这样会给 N.E.K.O 内置的 Python 3.11 安装匹配的 wheel，不会把 cp312 的 `.pyd` 装进去。

5. 找不到 Python / pip 失败时，插件仍可加载，点歌入口会返回中文提示，
   不会让整个插件崩掉。

### 手动重装依赖

```powershell
cd "$env:LOCALAPPDATA\N.E.K.O\.neko-plugin-installations\plugins\bilibili_music"
Remove-Item -Recurse -Force .\_vendor
python bootstrap_deps.py --target-python 3.11
```

如果系统找不到 Python，先安装一个 Python 3.11（不必和 N.E.K.O 在同一目录）。

---

## 可选：B 站 Cookies

B 站风控严格时，搜索或解析可能失败。可以把浏览器导出的 cookies 放到插件的
**数据目录**，文件名：

```text
bilibili_cookies.txt
```

支持两种格式：

- Netscape `cookies.txt`（浏览器扩展导出）；
- 一行纯文本：

```text
SESSDATA=xxxx; bili_jct=xxxx; buvid3=xxxx
```

插件会用它做搜索请求、传给 yt-dlp，并在下载音频时带上。

⚠️ Cookies 等同于账号凭证，不要发给别人，也不要提交到 GitHub。

---

## 缓存

- 下载目录：`static/audio/`
- 文件名：`<bvid>.m4a`
- 同一首歌重复点播会直接复用缓存。
- 想清理就删除 `static/audio/` 下的文件，下次点歌会重新下载。
- 音频文件较大时（单首几 MB 到十几 MB），请留意磁盘空间。

---

## 常见问题

### 启动报 `start_plugin failed`，插件日志为空

插件放错目录了。必须放用户插件目录：

```text
%LOCALAPPDATA%\N.E.K.O\.neko-plugin-installations\plugins\bilibili_music
```

不要放 `resources\bin\plugin\plugins`。放好后在插件管理器刷新/重载。

### 报 `ModuleNotFoundError: No module named 'requests'` / `yt_dlp`

依赖没装到 `_vendor`。运行：

```powershell
python bootstrap_deps.py --target-python 3.11
```

然后看 `_vendor\install.log`。

### `DLL load failed` / `.pyd` 无法导入

目标 Python 版本选错了。`--target-python` 必须和 N.E.K.O 内置版本一致
（当前是 3.11）。删掉 `_vendor` 后按正确版本重装。

### 搜索超时 / 连不上 B 站

检查系统代理、DNS 和网络。插件搜索超时是连接 6 秒、读取 15 秒。

### B 站返回 412 / -352

触发风控。等几分钟再试，或按上面的方法放 cookies。

### 歌找到了但没声音

按顺序检查：

1. 插件日志里有没有 `下载音频` 和 `音频下载完成`；
2. `static/audio/` 下有没有对应的 `.m4a`；
3. 访问 `http://127.0.0.1:48916/plugin/bilibili_music/ui/audio/<文件名>.m4a`
   是否能播放；
4. N.E.K.O 日志里是否有 `music_play_url broadcasted`；
5. 前端音乐播放器是否被其他窗口占用。

### 为什么只取第一首？

当前版本刻意保持简单：搜索第一条视频，不提供候选列表。
如果第一首不符合预期，可以在点歌时加上歌手名，或后续扩展成多候选选择。

### 支持网易云 / QQ 音乐吗？

不支持。这个插件只做 B 站音源。其他音源可以看 N.E.K.O 自带的
`music_pusher` 或官方点歌台。

---

## 兼容性

- 当前在 **Windows + Steam 版 N.E.K.O（Python 3.11 运行时）** 实测通过。
- Linux / macOS 未测试。
- N.E.K.O 更新后如果播放协议变化，可能需要同步调整
  `music_play_url` / `music_allowlist_add` 的字段。
- 搜索接口、B 站风控策略变化时，先升级 yt-dlp：

```powershell
python bootstrap_deps.py --target-python 3.11
```

---

## 免责声明

- 本项目仅用于个人学习、研究与自用娱乐。
- 音频版权归 B 站视频作者及版权方所有，本项目不存储、不分发任何音源。
- 请遵守 B 站用户协议、N.E.K.O 使用条款以及当地法律法规。
- 因使用本插件产生的版权、账号或网络问题，由使用者自行承担。
- 本项目与 N.E.K.O 官方、哔哩哔哩官方均无隶属关系。

---

## 致谢

- [N.E.K.O](https://github.com/Project-N-E-K-O/N.E.K.O)：插件 SDK 与播放器协议
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)：音频流解析
- [requests](https://github.com/psf/requests)：HTTP 请求
- 内置 `music_pusher` 插件：`music_play_url` / `music_allowlist_add` 协议参考

---

## 更新记录

### v1.0.0

- 初始版本：B 站搜索、yt-dlp 音频解析、本地缓存、N.E.K.O 原生播放协议。
- 支持冻结应用依赖自注入。
- Windows + N.E.K.O Python 3.11 实测通过。

---

## License

暂未指定开源许可证。发布到 GitHub 前建议补充一个 `LICENSE`
（例如 MIT）；在补充之前，默认保留所有权利。