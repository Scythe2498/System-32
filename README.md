# System 32

**A private AI assistant that lives on a USB drive.** Plug it into a Windows PC and it sets itself up, runs entirely on that computer, and takes nothing about you anywhere else. Unplug it, and your assistant goes with you.

System 32 chats, sees, speaks and listens. It generates images, video and 3D models, finds and organises your files, writes and runs code, searches the web privately, and guards your network — all on your own hardware, with no account, no cloud and no subscription.

> In a world where data collection is the norm, System 32 is built to break the cycle. Your conversations, files, memories and searches stay on your drive.

---

## Contents

- [What it can do](#what-it-can-do)
- [How it works](#how-it-works)
- [Privacy](#privacy)
- [Requirements](#requirements)
- [Getting started](#getting-started)
- [Using it day to day](#using-it-day-to-day)
- [Security: the Iron Dome](#security-the-iron-dome)
- [Updates](#updates)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## What it can do

**Talk and think**
- Chat with a local language model (any GGUF model, run with llama.cpp). Vision models can look at pictures you attach — and at your screen when you ask.
- Its reasoning is shown in the chat, collapsed under each reply, so you can see *why* it did something.
- Voice: it can speak its replies (Kokoro and Piper voices, many to choose from) and take spoken input.
- Memory: it remembers what matters across sessions — preferences, past work, where it left off — and compresses older memories over time.
- Rename it, and customise the chat's look, from the settings menu.

**Create**
- Images, video, image-to-video, background removal, upscaling and image-to-3D, through ComfyUI.
- Workflows are drag-and-drop: put any ComfyUI workflow `.json` (API format) in `Sys 32's Home\JsonFiles\` and it can run it. Nothing is hard-wired to particular workflows.
- Steps chain in one request — *"generate a fox on a hill, remove the background, then turn it into a 3D model"*.
- If a workflow needs a model or custom node you don't have, System 32 finds and downloads it (automatically from Hugging Face, Civitai and GitHub; anything else asks you first) and restarts ComfyUI when it's ready.
- Train LoRAs from your own image sets.

**Your computer**
- Finds anything on any drive — internal, spare or USB — by name, in seconds (*"find my red fox pictures"*, *"where's that Skyrim mod"*).
- Copies, moves, renames, makes folders and deletes — **always through a Confirm card first**. Nothing touches your files until you press Confirm. Deleted items go to the Recycle Bin, never straight to oblivion.
- Several file steps can go on one card (*"move everything from test1 into test2, then delete test1"*). They run in order and stop at the first problem; a delete is held back unless the earlier copy or move is verified on disk.
- Browse generated and downloaded images and videos in the built-in image browser.
- A game library for Steam and other launchers, plus Unreal Engine integration.

**Code**
- Writes, runs, edits and imports code projects, compares versions, and keeps a plan of what's done and what's next.
- Code never reaches into System 32's own private folders.

**The web — privately**
- Web, image and video search through its own private search engine (SearXNG), which runs on your machine and starts automatically. No search company sees a profile of you.
- Reads web pages, and downloads images and videos (YouTube and most video sites) — videos always ask for confirmation first.
- Every outbound request goes through one logged chokepoint, and **airplane mode** cuts System 32 off from the internet completely with one switch.

**Look after itself**
- Learns from its own mistakes: recurring tool errors become lessons it reads at the start of each session.
- Repairs common problems on its own at startup (corrupted caches after a rough unplug, a damaged Python environment, an out-of-date video downloader).
- Closes everything it started — the model, ComfyUI, search — however you close it.

---

## How it works

System 32 is a set of Python programs that run from the drive and talk to a few well-known open-source engines:

| Part | What it does |
|---|---|
| **System 32** (`system32.py` + `s32_*.py`) | The assistant itself: the chat page, tool use, memory, file tools, security, setup |
| **llama.cpp** (`llama-server.exe`) | Runs the language model on your GPU |
| **ComfyUI** | Runs image, video and 3D workflows |
| **SearXNG** | Private web search, run locally |
| **Kokoro / Piper** | Text-to-speech |

When you start it, the launcher:

1. finds (or downloads) Python;
2. runs **setup**, which discovers what this computer has and fetches what's missing — Python 3.12 and git if needed, the pinned llama.cpp server, ComfyUI (pinned version) and its environment with the PyTorch build that fits your graphics card and driver, the voice engines, the 3D viewer, and its own packages;
3. writes a fresh `config.json` for *this* computer (drive letters, folders, hardware);
4. starts the model, ComfyUI and private search, and opens the chat page at **http://localhost:7432**.

Because setup re-discovers everything at every start, the drive works on any Windows PC and under any drive letter.

### Folders on the drive

```
<drive>:\
├── Sys 32's Home\                    System 32 itself (setup adds its Python, ComfyUI, llama-server…)
├── Sys 32's comfyui resources\       ComfyUI models, custom nodes, inputs    (created for you)
├── LLM Storage\                      Your language models (.gguf)            (created for you)
├── System 32's Output\               Everything it makes or downloads        (created for you)
└── System 32's reference material\   Memories, settings, learned lessons    (created for you)
```

---

## Privacy

- **Local by design.** The model, image generation, voice and search all run on your computer. There's no account, no telemetry and no cloud service behind it.
- **A new computer is a new person.** Each computer the drive is used on gets its own private memory folder. Someone using the drive on another PC can't read, search or be shown yours.
- **Private search.** Searches go through SearXNG running on your machine; its caches are kept on the drive and wiped at every start.
- **Airplane mode** (in the Iron Dome panel) stops every web feature and blocks System 32's programs from the internet at the firewall. The header shows **✈ OFFLINE** while it's on.
- **Factory reset** (in the Data panel) wipes every trace of everyone who used the drive — memories, conversations, generations, downloads, settings, learned behaviour, and the firewall rules System 32 made — and leaves only the program and its models. The next start is a true first boot.

---

## Requirements

- **Windows 10 or 11**, 64-bit.
- **An NVIDIA graphics card** is strongly recommended. System 32 adapts its settings to how much video memory you have:

  | Tier | Video memory |
  |---|---|
  | high | 16 GB or more |
  | mid | 8–16 GB |
  | low | 4–8 GB |
  | minimal | under 4 GB |

  RTX 20-series and newer get the fastest PyTorch build; older cards (GTX 10-series and earlier) are supported with an older build. An up-to-date NVIDIA driver (580 or newer) gives the best performance.
- **RAM:** depends on the model you choose — 16 GB is a practical minimum; 32–64 GB for large models.
- **A fast USB drive (or any drive), formatted NTFS.** Plan on several hundred GB if you keep image, video and 3D models on it. NTFS matters — exFAT has no journaling, and an unplug at the wrong moment can corrupt files.
- **An internet connection for the first start** (it downloads its tools — several GB). After that it runs fine offline.

---

## Getting started

1. **Put System 32 on the drive.** Copy this repository onto the drive so `Sys 32's Home\` sits at the top level (or in any folder you like — everything is found relative to it).

2. **Add a language model.** Put a `.gguf` model in its own folder inside `LLM Storage`, next to `Sys 32's Home`. (If you start System 32 before adding one, setup creates `LLM Storage` with a note inside and tells you what to do — then start it again.) For example:
   ```
   LLM Storage\<publisher>\<model name>\<model>.gguf
   ```
   For vision, put the model's matching `mmproj-….gguf` in the same folder — System 32 detects it and turns vision on. If there are several models, it starts with the newest; switch any time with the model switcher (click the SYSTEM 32 logo).

3. **Start it.** Double-click **`Start.bat`** in `Sys 32's Home` (or a shortcut to it — see [A System 32 shortcut](#a-system-32-shortcut)).
   - The **first start** sets everything up by itself and takes a while — it downloads several GB: System 32's own Python, git and Python 3.12 if the computer doesn't have them, the llama.cpp server, ComfyUI and its environment (with the PyTorch build that fits your graphics card), the voice engines (Kokoro, plus the Piper program for voices you add yourself), private search and the chat page's 3D viewer. Each step is shown in the window. Don't close it while it says *Installing…* or *Downloading…*, even if it looks stalled on a slow connection.
   - When setup finishes, press **Enter**. The chat page opens at **http://localhost:7432**.
   - Later starts are quick.

4. **Say hello.** On a new computer, System 32 introduces itself and asks who you are.

5. **Add image models when you want them.** ComfyUI's models and custom nodes live in `Sys 32's comfyui resources\` (made for you). Drop workflow `.json` files (API format) into `Sys 32's Home\JsonFiles\`; when a workflow needs a model or custom node you don't have, System 32 fetches it.

### A System 32 shortcut

`Sys 32's Home` includes the System 32 icon (`System32.ico`), so you can make a proper shortcut — for the top of the drive, or your desktop:

1. Right-click **`Start.bat`** → **Show more options** (Windows 11) → **Create shortcut**, and move the shortcut where you want it.
2. Right-click the shortcut → **Properties**. Make sure **Target** is `"<drive>:\Sys 32's Home\Start.bat"` and **Start in** is `"<drive>:\Sys 32's Home"` (with your drive's letter, quotes included).
3. Click **Change Icon…** → **Browse…** → pick `Sys 32's Home\System32.ico` → **OK**.

A shortcut remembers a fixed drive letter. If the drive shows up under a different letter on another computer, fix the two paths the same way, or start `Start.bat` directly — that always works, whatever the letter.

### Moving between computers

Close System 32, unplug, plug into the next PC and start it again. Setup adapts to the new machine; if the new machine's Python is different, it repairs ComfyUI's environment in place rather than downloading everything again. Each computer keeps its own private memory.

---

## Using it day to day

Just ask, in plain language. A few examples:

- *"make an image of a cozy cabin in the snow, then turn it into a short video"*
- *"remove the background from this"* (with an image attached)
- *"find my gameplay videos from last month"*
- *"move all the pictures in Downloads into a new folder on the desktop called Sorted"*
- *"search the web for the latest llama.cpp release"*
- *"write me a script that renames my screenshots by date, and run it"*
- *"what's on my screen?"*

Anything that changes your files, downloads a video or takes a security action shows a **Confirm card** first. Press **Confirm** to go ahead, or **Cancel** — nothing happens until you choose.

**The panels** (in the menu): settings and model tuning, voice and vision, chat customisation, the image browser, the game library, the Data panel (exports and factory reset) and the Iron Dome.

**Closing:** close the browser tab, press Ctrl+C in the window, or close the window. Everything System 32 started closes with it.

---

## Security: the Iron Dome

System 32 includes a network guardian that watches the ports and connections on your PC and alerts you to anything suspicious. It **never acts on its own** — anything it wants to stop or block shows a card for you to confirm.

The **Iron Dome** goes further. It learns what normally runs and listens on your computer; once **armed**, anything new that opens a port or reaches out to a server it hasn't seen before is closed and blocked at the Windows Firewall. Windows' own programs are always left alone, and anything you trust can be whitelisted. An **outbound watcher** also learns which companies each program normally talks to, and flags (or, when armed, blocks) programs that suddenly talk to somewhere new or "phone home" on a timer.

It's deliberately aggressive: it's a firewall, not a toy.

**The Iron Dome needs administrator rights**, because it changes Windows Firewall rules. Without them, the arm button is hidden and the panel says so — System 32 won't pretend to protect you when it can't. To use it:

1. Close System 32.
2. Right-click **`Start.bat`** (or your shortcut to it) → **Run as administrator**.
3. Open the Iron Dome panel and press **ARM**.

System 32 doesn't need administrator rights for anything else, and doesn't ask for them.

---

## Updates

Once a day, System 32 asks this GitHub repository whether there's a newer release — one anonymous request, nothing about you or your computer, and never in airplane mode. When there is one, a notice appears at the top of the chat: **What's new**, **Update** or **Not now**.

**Update** replaces only System 32's own program files. Your memories, settings, outputs and models are never touched. The files it replaces are kept, and a **Roll back** button appears after the restart in case you want the old version back. System 32 restarts by itself.

To turn the check off, add `"update_check": false` to `Sys 32's Home\config.json`.

---

## Troubleshooting

- **The setup window seems stuck on "Installing PyTorch…"** — it's downloading about 2–3 GB. On a slow connection this takes a while; leave it running.
- **"Access is denied" when running ComfyUI's Python** — the environment's launcher was damaged. Restart System 32; setup repairs it automatically without re-downloading.
- **A folder you just made isn't found** — search falls back to checking your Desktop, Documents, Downloads, Pictures and Videos directly, so it should still turn up. Telling System 32 where to look always works.
- **Logs:** the startup window itself, plus `llama_server.log` and `node_deps.log` in `Sys 32's Home\` and `system32_image.log` (ComfyUI) in `Sys 32's Home\ComfyUI\`. They're the first thing to check when something misbehaves.

---

## License

System 32 is **source-available** under the [PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/). Anyone may use, study, modify, fork and share it for noncommercial purposes. It is not "open source" in the OSI sense, because selling it isn't allowed.

**Forks may accept tips and donations; selling System 32 or any fork of it is not permitted.**

System 32 works alongside other projects that keep their own licenses — including llama.cpp (MIT), ComfyUI (GPL-3.0), SearXNG (AGPL-3.0), yt-dlp (Unlicense), Kokoro (Apache-2.0) and kokoro-onnx (MIT), Piper (MIT), and the 3D viewer (Google's model-viewer, Apache-2.0, with three.js, MIT). None of them is included in this repository: System 32 downloads each from its official source the first time it's needed. Models, voices and custom nodes each carry their own licenses as well; check them before using or sharing what you make with them.

---

*Created by SoullessScythe — built over many late nights, one iteration at a time.*
