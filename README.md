# Online Course Assistant

**Windows 网课助手 · 智慧树 / 超星学习通 / 智慧职教**

[![最新版本](https://img.shields.io/github/v/release/LLL-svg-jpg/Online-Course-Assistant)](https://github.com/LLL-svg-jpg/Online-Course-Assistant/releases/latest)
![Windows x64](https://img.shields.io/badge/Windows-x64-0078D6)
![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB)

一款基于 Python、Playwright 和 Tkinter 的桌面学习辅助工具，提供多账号登录、按课程地址绑定账号、视频播放、课件阅读和部分 AI 答题辅助。

A Windows online course assistant for Zhihuishu, Chaoxing and ICVE, with per-course accounts, isolated browser sessions and optional AI answer assistance.

**[下载 Windows 版](https://github.com/LLL-svg-jpg/Online-Course-Assistant/releases/latest)** · [快速开始](#快速开始) · [支持范围](#支持范围) · [问题反馈](https://github.com/LLL-svg-jpg/Online-Course-Assistant/issues)

## 能做什么

- **多账号管理**：每条课程地址选择对应账号，切换时隔离登录会话。
- **课程播放与课件阅读**：支持部分课程章节目录首页和视频播放页，可调倍速、静音。
- **登录等待控制**：登录或验证超时后跳过当前地址，继续下一条；可随时停止。
- **可选 AI 答题**：可配置服务商、接口和模型，配合本地题库辅助部分题目流程。
- **本机保存**：配置、登录状态、题库和日志保存在本机。
- **手动更新**：在“设置 → 关于”检查正式版本，选择更新后在原 EXE 目录安装并保留本机数据。

## 快速开始

1. 在 [Releases](https://github.com/LLL-svg-jpg/Online-Course-Assistant/releases/latest) 下载对应版本的 `OnlineCourseAssistant-v版本号-windows-x64.zip`。
2. 完整解压，运行 `OnlineCourseAssistant.exe`。保留同目录的 `_internal`，电脑需要安装 **Chrome 或 Edge**，无需安装 Python。
3. 在“课程”页粘贴课程章节目录首页或视频播放页的完整地址，每行一个。平台按地址自动识别。
4. 点击“新增”填写账号名称、登录账号和密码，在每条地址右侧选择账号；也可留空凭据，使用浏览器手动登录。
5. 点击“保存配置”，再点击“开始刷课”。结束时点击“停止”。

登录与验证等待时限在“设置 → 运行”中调整。AI 功能需要自行配置 API Key，服务商调用可能收费。

## 支持范围

| 平台 | 已适配的主要流程 |
| --- | --- |
| 智慧树 / 知到（Zhihuishu） | 登录、共享课目录、视频播放、部分视频弹题与平时测试；混合列表跳过正式考试 |
| 超星学习通（Chaoxing） | 登录后进入个人空间、课程章节目录、视频、部分章节测验与独立考试页面 |
| 智慧职教（ICVE） | SSO 登录、旧版学习空间目录、视频及 PPT/PDF 课件 |

网课播放和刷课辅助仅覆盖已适配的页面结构。学习通直播、未知验证类型、智慧职教全部新版课程业务尚未覆盖；视频弹题和 AI 作答也有平台适配限制。

登录和部分验证码自动处理已有有限真实样本验证，平台变化后可能需要人工处理。正式考试请人工检查，默认不自动交卷。页面播放、登录成功和服务器计入学时是不同结果，不能保证整门课完成。

独立测试没有 AI Key（含环境变量）且没有题库参考时不会猜答案；已有答案会保留，题库有参考时仍可填写。视频弹题的“答错自动重试”是独立开关；要完全关闭自动答题，请关闭“启用自动答题”。

已知界面问题：字号更新仍可能有短暂重绘过渡，关于页依赖检查按钮在部分启动中漏绘。本次改名发布不扩大既有功能验收范围。

## 配置与升级

- `config.toml`：账号、课程地址、界面与运行设置。
- `runtime/`：登录状态、本地题库和日志。

从 v1.0.2 起，可在“设置 → 关于 → 检查更新”查看最新正式版本。确认更新后，程序下载并核对发布附件的大小、SHA-256 和 EXE 版本，退出后原地替换 EXE、完整 `_internal` 与说明文件，再重新打开。`config.toml`、`runtime/` 及其它个人文件保留；旧程序备份和更新结果存于 `runtime/updates/`，替换失败时回退。

旧版本或源码运行请手动升级：退出旧版本，把新版本解压到新目录，再按需复制自己的 `config.toml` 和 `runtime/`，保留旧版本便于回退。源码目录不能用 EXE 更新包覆盖。

这些文件可能含有明文凭据和个人学习记录，**只留本机，请勿上传或分享**。公开源码与下载包不包含个人配置、Cookie、题库、日志、截图、测试脚本或本机交接材料。

## 常见问题

**“默认账号”的账号密码是什么？**

没有内置账号或密码。“默认账号”是旧配置的兼容名称；请选择账号后填写凭据并保存。

**能粘贴平台的总课程列表地址吗？**

请使用某门课程的章节目录首页或播放页。平台总课程列表与课程首页是不同页面，当前不能将任意平台首页自动转换成学习任务。

**为什么出现验证后仍需要我操作？**

验证类型和平台策略会变化。自动处理不覆盖所有情况，可在等待时限内手动完成；验证控件消失也不单独代表认证成功。

## 源码运行

需要 Python 3.11 或更高版本，以及 Chrome 或 Edge：

```powershell
$env:PYTHONUTF8 = '1'
python -m pip install -r requirements.txt
python OnlineCourseAssistant.pyw
```

`安装依赖.bat` 仅用于源码环境，不能修复缺少内部依赖的 EXE。完整发布构建依赖维护者本机测试资料，测试和交接材料不公开分发。

## 反馈与第三方说明

在 [Issues](https://github.com/LLL-svg-jpg/Online-Course-Assistant/issues) 中说明软件版本、平台、复现步骤和预期结果。请先去除账号、课程地址、密码、API Key 与个人学习记录。

第三方轨迹参考实现的来源和许可见 [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt)。
