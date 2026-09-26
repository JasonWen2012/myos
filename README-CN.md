# myos

一个从零写起的 x86 操作系统，在自己写的引导器和自己的链接器上跑出两个内核：

| | 16 位内核 | 32 位内核 |
|---|---|---|
| 运行方式 | 实模式，自研模拟器就能跑 | 保护模式，C++（`g++ -m32`），用 QEMU 跑 |
| 现在能做什么 | 引导、打字、命令行 | 上面这些 + 中断/定时器/串口 + **自己的硬盘驱动和文件系统** |
| 能不能存文件 | 不能 | 能，重启后还在（存在 `data/myfs-data.img`） |

想直接玩：**跳到 [5 分钟上手](#5-分钟上手)**。想先知道"这是什么、为什么这么写"，看
[`docs/design.md`](docs/design.md)。

---

## 5 分钟上手

在项目根目录（`myos`）用 **cmd 或 PowerShell** 执行。构建只要 Python（标准库），不需要 make。

### 路线 A：16 位内核（不用装任何虚拟机）

```bat
python build.py all
python run.py --interactive
```

你会看到内核启动、打出横幅，然后停在 `myos>`。随便打：

```
help
info
mem
fact 5
reboot
```

按 `Ctrl+C`（或 `Esc`）退出交互。这个内核跑在**项目自带的 16 位模拟器**里，所以没有
任何外部依赖，也不需要显卡和权限。

### 路线 B：32 位内核（能存文件，用 QEMU）

```bat
python build.py all --arch 32
python build.py data-disk
python run.py --arch 32
```

会弹出一个 QEMU 窗口（里面是内核的 VGA 画面），同时这个终端里也打印同样的文本。
在 QEMU 窗口里打字（或者直接在这个终端里打字，两个都是输入）：

```
help
ls /
cat /readme.txt
write /note.txt hello myos
cat /note.txt
df
reboot                      ← 关机前会自动把卷标记干净
```

**然后重新跑一次 `python run.py --arch 32`，再打 `ls /` 和
`cat /note.txt`——文件还在。** 这就是"长期保存"：不停留在内存里，真的写进了
`data/myfs-data.img`。

在 Windows 里也能直接看那个盘：

```bat
python tools/myfs.py --image data/myfs-data.img --partition 1 --list
python tools/myfs.py --image data/myfs-data.img --partition 1 --extract /note.txt --out note.txt
```

`myfs.py` 是文件系统格式的**另一份实现**（Python 写的），内核里是 C++ 那份。两边互相
校验：构建时会算好每个文件的校验和写进 `/manifest`，内核每次启动都重算一遍。

### 看看它内部长什么样

```bat
python memmap.py --arch 32 --image    :: 磁盘/内存布局（LBA、分区、卷都列出来）
python memmap.py --live               :: 启动一次 16 位内核，打印运行时内存快照
python build.py doctor                :: 报告工具链探测结果（NASM/g++/QEMU 找没找到）
```

---

## 打字和使用规则

- **只支持 ASCII**：键盘和串口都只接受可见 ASCII（码 32..126）。目前打不了中文。
- 回车提交（`Enter` / `\r` / `\n` 都行），退格可以删字符，一行最多 79 个字符、最多 8 个词。
- 命令名**不区分大小写**（`HELP` 和 `help` 一样），参数区分。
- 退出：16 位交互按 `Ctrl+C`；32 位关掉 QEMU 窗口，或在 guest 里打 `reboot`。
- 32 位不需要额外开关：`python run.py --arch 32` 就能打字（`--interactive` 是给 16 位
  模拟器用的）。QEMU 窗口和这个终端**同时**显示输出，输入也可以来自任何一边。

## 命令速查

### 16 位内核

| 命令 | 作用 |
|---|---|
| `help` | 命令列表 |
| `echo <text>` | 原样回显 |
| `clear` | 清屏 |
| `info` | 版本、镜像大小、栈指针、段寄存器 |
| `mem` | 固件报告的内存 / myos 自己占用的内存 |
| `ticks` | BIOS 的 18.2 Hz 计时（十进制 + 十六进制） |
| `fact <0-8>` | 递归阶乘（演示栈和调用约定） |
| `keylog` | 谁拥有 INT 09h、为什么这决定输入能不能用 |
| `reboot` | 重启 |

### 32 位内核：通用

| 命令 | 作用 |
|---|---|
| `help` `echo` `clear` `mem` `ticks` `fact` `keylog` `reboot` | 和 16 位内核一样 |
| `info` | 版本、镜像/入口地址、GDT/IDT、PIC 向量、引导盘号 |
| `selftest` | 跑内核自检（28 项）并**用退出码报告结果**，测试靠它判定 |

### 32 位内核：磁盘和文件

| 命令 | 作用 |
|---|---|
| `ls [路径]` | 列目录（默认 `/`），目录显示条目数，文件显示大小和 inode 号 |
| `cat <路径>` | 打印文件内容（先报大小，内容后面不多加东西） |
| `write <路径> <文本>` | 创建或**覆盖**文件；不给文本就是清空 |
| `mkdir <路径>` | 建目录 |
| `rm <路径>` | 删文件或**空**目录（非空会拒绝，根目录也拒绝） |
| `stat <路径>` | 类型 / inode / 大小 / 占用块数 |
| `df` | 卷用了多少、还剩多少（块和 inode 分开报） |
| `fs` | 卷的版本、卷标、状态（干净/脏）、分区位置 |
| `blk` | 硬盘型号、容量、分区表里的 myfs 卷 |
| `blk test` | 往硬盘尾部"测试区"写图案再读回比对（唯一会写盘的自检） |
| `sync` | 把卷标记为"干净卸载"（写入本来就是直写的，没有缓冲要刷） |
| `fsck` | 扫描整个卷：回收漏块、修正空闲计数、清掉脏标记 |
| `fstest` | 内核自己的写路径自检（22 项），跑完把卷恢复原样 |

一次真实的会话大概长这样：

```
myos> fs
fs: myfs v1 'myos', 2048 blocks of 512 bytes, 64 inodes
fs: state clean, 2036 free blocks, 57 free inodes
myos> write /note.txt hello myos
write: /note.txt, 10 bytes, 2035 free blocks
myos> cat /note.txt
cat: /note.txt (10 bytes)
hello myos
cat: 10 bytes read
myos> df
df: 2048 blocks of 512 bytes: 7 used, 2035 free
df: 64 inodes: 7 used, 56 free
```

---

## 文件到底存在哪

| 位置 | 是什么 | 会不会被构建覆盖 |
|---|---|---|
| `files/` | 仓库里预置的文件，会被打包进**构建产物** | 每次 `build.py` 重新打包 |
| `images/myos32-hd.img` | 构建出来的硬盘镜像（含 myfs 卷），测试用它 | 会被 `build.py all` 重写 |
| `data/myfs-data.img` | **你自己的盘**，`run.py --arch 32` 挂的就是它 | **永远不会**被构建覆盖 |

所以：想留东西就写在 `data/` 那块盘上（也就是在 guest 里 `write` 到 `/`）。`files/` 里
的文件是"随固件发布"的示例文件；`build.py clean` 只删 `build/` 和 `images/*.img`，
不会动 `data/`。

卷的规格（`myfs` 格式）：

- 1 MiB = 2048 块 × 512 字节，64 个 inode。
- 单个文件最大 **136704 字节**（11 个直接块 + 1 个一级间接块）。
- 文件名最长 **29 个字符**，按原样比较（区分大小写）。
- 支持子目录，但**不支持 `..`**（格式里没存父指针，不能瞎猜）。
- 只有**覆盖写**，没有追加写；没有时间戳和权限。

## 目录结构

```
boot/          引导器：boot16.asm（512 字节引导扇区）+ stage2.asm（加载内核）
kernel16/      16 位内核（纯汇编：控制台、键盘、shell）
kernel32/      32 位内核（C++：GDT/IDT/PIC/PIT、键盘、串口、ATA 驱动、myfs、shell）
emulator/      自研 16 位 x86 模拟器 + BIOS + VGA 文本渲染（16 位内核靠它验收）
tools/         cofllink.py 自研链接器、myfs.py 文件系统工具、qemu.py 测试后端、image.py 镜像
files/         例子文件：构建时打包进 myfs 卷，并生成 /manifest 校验清单
tests/         回归测试（194 个）
build.py       构建：汇编 + 链接 + 打包镜像；也负责创建你的数据盘
run.py         运行镜像：按镜像头自动选后端（16 位→模拟器，32 位→QEMU）
memmap.py      打印内存/磁盘布局，数字都从源码和产物里量出来
docs/design.md 设计与踩坑记录（想深入看这个）
```

## 常用命令一览

```bat
python build.py all                     :: 16 位：引导扇区 + stage2 + 内核 + 软盘/硬盘镜像
python build.py all --arch 32           :: 32 位：同上，外加 myfs 卷和 2 MiB 硬盘镜像
python build.py data-disk               :: 创建 data/myfs-data.img（已存在就不重建）
python build.py doctor                  :: 工具链探测
python build.py clean                   :: 清掉 build/ 和 images/*.img（不动 data/）

python run.py                           :: 自研模拟器跑 16 位并打印屏幕
python run.py --interactive             :: 16 位交互（Ctrl+C 退出）
python run.py --arch 32                 :: 32 位：QEMU 窗口 + 自动挂上你的数据盘，直接打字
python run.py --arch 32 --no-build      :: 不重新构建，直接跑现有镜像
python run.py --dump                    :: 附带寄存器/机器状态

python memmap.py --arch 32 --image       :: 磁盘布局
python memmap.py --live                  :: 16 位运行时内存快照
python tools/myfs.py --image data/myfs-data.img --partition 1 --list
python tools/myfs.py --image data/myfs-data.img --partition 1 --extract /note.txt --out note.txt
python tools/myfs.py --image data/myfs-data.img --partition 1 --check
```

## 测试：怎么知道它没坏

```bat
python -m unittest discover -s tests          :: 全部（194 个）
python -m unittest discover -s tests -v       :: 带每条用例名
python -m unittest tests.test_kernel32 -v     :: 只跑 32 位那组
python -m unittest tests.test_myfs            :: 只跑文件系统格式那组
```

- **16 位**用自研模拟器验收：确定性高、不需要 QEMU。
- **32 位**用 QEMU 验收：真实固件、真 CPU。自检通过 `isa-debug-exit` 设备把结果变成
  进程退出码（`0x21` = 通过），所以"绿灯"是 guest 自己说的，不是测试等到超时。
- **没装 QEMU** 的机器上，32 位那部分测试会 **skip**（不是 fail）。
- 文件系统部分用"两次独立启动"验收持久化，并且**内核写、主机读**互相验证。

---

## 常见问题

**为什么 32 位必须用 QEMU？**
因为项目自带的模拟器只实现到 16 位实模式（32 位寄存器、描述符、异常都还没写）。
`run.py` 会读镜像头里的架构字节自动选后端；你要是强行让模拟器跑 32 位镜像，它会明确
报错而不是跑飞。把保护模式补进自研模拟器是后续计划之一。

**我写的文件不见了？**
两种可能：
1. 你写的是构建产物（`images/` 里的盘）。每次 `build.py` 都会重写它们。
2. 你写在了 `data/myfs-data.img` 上，但用 `python run.py`（默认 16 位）启动——16 位
   内核没有文件系统，也看不到那个盘。用 `--arch 32`。

**能输入中文吗？**
不能：键盘扫描码解码和串口输入都只接受 ASCII（32..126）。文件内容里也不会有非 ASCII。

**怎么退出？**
16 位交互：`Ctrl+C`。32 位：关掉 QEMU 窗口，或者在 guest 里打 `reboot`（它会把卷标记
成干净卸载再重启，所以直接关机也不会让下次启动报警）。

**没装 QEMU 会怎样？**
16 位照常用（自研模拟器）。32 位会提示找不到 `qemu-system-i386`；测试里 32 位那组会
skip。装一个（Windows 版 QEMU，`qemu-system-i386.EXE` 在 `PATH` 或
`C:\Program Files\qemu\`）就行。

**`ls` 说"no filesystem mounted"？**
没挂上盘时会这样，括号里是原因（`no ATA device on the primary channel` = 这次启动没
给 guest 挂硬盘）。用 `run.py --arch 32` 启动就会自动挂上 `data/myfs-data.img`。

**为什么 `..` 不能用？**
格式里没存父指针，返回"不支持"比猜一个目录更诚实。

**块、inode 是什么？**
见 [`docs/design.md` 的术语表](docs/design.md#术语表)。

**怎么改内核、加一条命令？**
16 位：`kernel16/shell.asm` 里加处理函数 + 在 `kernel16/main.asm` 的命令表加一行（表里
存的是"相对表头的偏移"）。32 位：`kernel32/shell.cpp` 里写一个 `cmd_xxx()`，在
`commands[]` 表里加一行，再在 `cmd_help` 上面加上声明。改完 `python build.py all
--arch 32` 重新构建；跑一遍测试确认没踩到别的东西。

**32 位内核多大、还有多少余量？**
引导器给内核留了 896 个扇区（448 KiB）；现在用了大约 160 个扇区。`build.py` 会在超预算
时直接构建失败，而不是产出一个坏镜像。

## 现在还没有的

分页与堆（`kmalloc`）、进程/线程、用户态和系统调用、多磁盘、时间戳和权限、追加写、
大于 136704 字节的文件、中文输入、以及 64 位（目前没有这个计划）。完整的设计理由和
一路踩过的坑在 [`docs/design.md`](docs/design.md)。

## 环境要求

| 需要 | 用来干什么 | 这条机器上的情况 |
|---|---|---|
| Python 3 | 构建、运行、测试脚本（不用 make/cmake） | 有 |
| NASM | 汇编引导器和 16 位内核 | `C:\Users\JasonW\AppData\Local\bin\NASM\nasm.exe` |
| MSYS2 UCRT64 `g++` | 编译 32 位内核（`-m32`） | 16.1.0 |
| QEMU (`qemu-system-i386`) | 只跑 32 位内核和它的测试 | `C:\Program Files\qemu\` |

本项目**不使用 WSL**（试过多次不成功），全部工具链都是原生 Windows；没有 `ld` 能产裸机
镜像，所以 32 位内核用项目自己的链接器 `tools/cofllink.py` 链接。`python build.py doctor`
会告诉你哪个工具没找到。
