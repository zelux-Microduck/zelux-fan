# fanctl —— 可配置的 PWM 风扇散热控制

> 2026-11-18 集成说明：在 ZELUX Device Image 中，请仅使用 zelux-fand.service；规范组件路径为 /opt/zelux/current/software/zelux-fan/fanctl，TOML 为 /etc/zelux/zelux.toml，socket 为 /run/zelux/fan.sock，实例清单为 /var/lib/zelux-fan/inventory.json。下面的独立 install.sh/fanctl.service 是非镜像环境入口，与镜像服务互斥。
>
> 当前服务只使用已预导出的 PWM，不自动 export/unexport；禁用和删除停止输出但保留 PWM 节点。预导出流程和实际接线必须由板级部署确认。set 只覆盖指定实例，其他风扇继续温控；全局 mode 保持 auto，实例 control_mode 显示手动覆盖，auto 清除全部覆盖，重启恢复自动。curve 更新持久化。新增 enable/disable 操作用稳定 instance_id；delete 必须 confirm=true。无测速时自检为未知，不能当作通过。
>
> 本地代码已完成本轮回归；尚未进行设备及浏览器验收，fan 仓库仍为 local-snapshot，未正式发布。

这个包负责**让配置中的一个或多个 PWM 风扇按温度调速**。驱动实现按型号复用，风扇数量由配置中的 `[[fan.channel]]` 实例数决定。

一个 Python 文件、**零第三方依赖**（只用标准库），装完就是一个 systemd 服务，
插上就从 60% 起步、温度上来自己加速、温度下去自己收油门，不用你管。

```
温度 <58℃ ──► 60%          （默认档，不停转，省得重启时"咯"一声还积热）
 ≥58℃ ─────► 70%
 ≥64℃ ─────► 85%
 ≥70℃ ─────► 100%          （全速）
```

---

## 1. 它能做什么

| 能力 | 说明 |
|---|---|
| **自动温控** | 每 2 秒读一次 CPU 温度（取所有 `thermal_zone` 里最高的），套阶梯曲线写 PWM |
| **手动调速** | 命令行按兼容序号控制；新版 socket 用稳定实例 ID 指定风扇 |
| **手动保护** | 手动值默认持 120 秒后交回自动，防止手滑点了 100% 忘了改回来 |
| **实例隔离** | 手动调速只影响指定实例，全局模式保持 auto，其他实例继续温控 |
| **转速自检** | 全速 1 秒读 tach，判断风扇到底转没转（这是唯一靠谱的判据） |
| **远程控制** | 常驻一个 unix socket（一行一个 JSON），给网页 / 别的程序调用 |
| **不崩** | 没有风扇、没有 PWM 通道、读不到温度，都返回人话原因，绝不抛异常 |

---

## 2. 适用范围（先看清楚）

**PWM/sysfs 后端当前面向 Radxa Zero 3W / RK3566**。可配置一颗或多颗风扇实例；每颗实例的 PWM/tach 映射、周期和测速参数均由配置给出，不应把示例引脚当作目标设备的实物接线结论。

换别的板子 / 别的针脚**不是不能用**，但要改 `/etc/fanctl.toml` 里的通道表。改法见 §5。

| 硬件 | 值 |
|---|---|
| 主控 | Radxa Zero 3W（RK3566，ARM64） |
| 风扇数量 | 配置实例数决定；每个实例使用唯一稳定 ID |
| PWM 参数 | 每实例配置 period；常见值 40000 ns（25 kHz），必须按风扇/板卡确认 |

> ⚠ 前提：**设备树里所配置的 PWM 通道得先 probe 出来**（`/sys/class/pwm/` 下能看到 `pwmchipN`）。
> Armbian 一般靠 overlay 开 PWM，具体怎么开取决于你的镜像。
> fanctl 只负责"往已经出现的通道写值"，**不替你改设备树**。
> **fanctl 不会 export PWM 通道** —— 只使用内核或设备树已经预先导出的通道，避免误控错误引脚。
> 装完先看 `ls /sys/class/pwm/pwmchipN/` 看有没有已经导出的 `pwmM` 目录。

---

## 3. 安装

```bash
# 1) 把整个目录拷到板子上（U 盘 / scp 都行），然后：
cd zelux-fan
sudo ./install.sh
```

装的时候它会：
1. 检查 python3（要 3.9+；3.11+ 用自带 `tomllib`，以下用内置的极简 TOML 解析器）和 PWM / thermal 目录在不在；
2. 装 `fanctl` 到 `/usr/local/bin/`，写默认配置到 `/etc/fanctl.toml`；
3. 装 `fanctl.service` 到 `/etc/systemd/system/`，`enable --now`；
4. 立刻打印服务状态 + 风扇状态 + 跑一次自检。

只想装文件不启动：`sudo ./install.sh --no-start`
先看会做什么不真做：`sudo ./install.sh --dry-run`

**没有 systemd 的机器**（比如某些精简 rootfs）也能用，直接把 `fanctl` 拷到
`/usr/local/bin/`，然后手工跑 `sudo fanctl --auto &` 或写进你自己的 rc。

**ZELUX 镜像集成**：镜像中 fanctl 安装到 `/opt/zelux/current/software/zelux-fan/fanctl`，
配置文件为 `/etc/zelux/zelux.toml`。独立安装使用上述路径。

---

## 4. 用法

### 4.1 最常用三条

```bash
sudo fanctl --status      # 看每路的转速 / 百分比 / 通道路径 / 当前温度
sudo fanctl --selftest    # 全速 1 秒 → 读转速 → 回原值（验风扇转不转）
sudo fanctl --set 0 80    # 第 0 路手动 80%（120 秒后自动交回温控）
```

`--status` 长这样：

```json
{
  "ok": true, "mode": "auto", "temp": 52.3,
  "items": [
    { "index": 0, "id": "cooling-top", "name": "顶部风扇", "pct": 60, "duty": 24000, "rpm": null,
      "control_mode": "auto", "path": "/sys/class/pwm/pwmchip3/pwm2", "why": "" },
    { "index": 1, "id": "cooling-main", "name": "主板散热", "pct": 80, "duty": 32000, "rpm": 3120,
      "control_mode": "manual", "path": "/sys/class/pwm/pwmchip3/pwm0", "why": "" }
  ]
}
```

* `control_mode`: 该实例的控制模式（`auto` 或 `manual`），手动调速仅影响该实例；
* `rpm: null` 且那颗本来就没测速线 → 正常，别当成故障；
* `path: null` → PWM 通道没找到，看 `why` 字段，按 §5 改配置。

### 4.2 全部命令

| 命令 | 作用 |
|---|---|
| `fanctl --serve` | 常驻：自动温控 + socket（systemd 用的就是这个） |
| `fanctl --auto` | 前台跑自动温控，Ctrl-C 退出（调试看日志用） |
| `fanctl --status` | 打印状态后退出 |
| `fanctl --selftest` | 全速自检 |
| `fanctl --set INDEX PCT` | 手动设某一路百分比 |
| `fanctl --once '{"op":"set","index":1,"pct":60}'` | 单条 JSON 请求 |
| `fanctl --stdin` | 从标准输入逐行读 JSON（SSH 里手动敲着玩） |
| `fanctl --config PATH` | 用指定配置文件 |
| `fanctl -v` | 详细日志 |

> 除了 `--status`，其它**写操作都要 sudo**（`/sys/class/pwm` 只有 root 能写）。

### 4.3 远程控制（unix socket）

独立安装默认开在 `/run/fanctl/fan.sock`；ZELUX 集成使用 `/run/zelux/fan.sock`。协议：**一行一个 JSON 请求，回一行 JSON**。API v1 新请求带 `api_version: 1`，通过 `instance_id` 标识风扇；旧 `index` 仍作迁移期兼容。

| 请求 | 应答 | 说明 |
|---|---|---|
| `{"op":"ping"}` | `{"ok":true,"pong":true,...}` | 探活 |
| `{"op":"status"}` | 完整状态 | 同 `--status` |
| `{"op":"get","index":1}` | 单路详情 | 只想问某一路时用 |
| `{"op":"temp"}` | `{"ok":true,"temp":52.3}` | 当前最高温 |
| `{"op":"set","index":0,"pct":80}` | `{"ok":true,...}` | 手动 80% |
| `{"op":"set","index":1,"duty":30000}` | `{"ok":true,...}` | 直接给 duty 也行 |
| `{"op":"set","index":0,"pct":80,"hold":30}` | 30 秒后交回自动 | 临时覆盖 |
| `{"op":"auto"}` | 交回自动 | |
| `{"op":"curve","points":[[60,50],[75,100]]}` | 临时改曲线（重启服务失效） | |
| `{"op":"selftest"}` | 自检结果 | |
| `{"api_version":1,"op":"enable","instance_id":"cooling-main"}` | 启用实例 | 启用已禁用的实例 |
| `{"api_version":1,"op":"disable","instance_id":"cooling-main"}` | 禁用实例 | 禁用实例并释放 PWM |
| `{"api_version":1,"op":"delete","instance_id":"cooling-main","confirm":true}` | 删除实例并返回释放资源 | 必须显式确认；只释放本服务拥有的资源 |

调用示例见 `examples/socket-call.sh`。

**权限**：socket 默认 `0600`（只有 root 能连）。要让普通用户/服务能调，
在 `/etc/fanctl.toml` 里设 `socket_group = "gpio"`（换成你有的组），
再把用户加进那个组，然后 `sudo systemctl restart fanctl`。

**不用远程控制**就把 `socket_enable = false` —— 那样 fanctl 只跑自动温控，
不监听任何 socket，暴露面最小。

---

## 5. 改配置（`/etc/fanctl.toml`）

静态硬件引脚配置改完需重启：`sudo systemctl restart fanctl`。ZELUX Agent 通过 socket 的 `create` / `update` / `delete` 管理运行清单；inventory 保存在 root 服务配置指定的文件中。

### 5.1 先确认通道路径

`--status` 里 `path` 是 `null`，说明 chip/channel 猜错了。查：

```bash
ls /sys/class/pwm/                 # 有哪些 pwmchip
cat /sys/class/pwm/pwmchip*/npwm   # 每个 chip 几路
ls /sys/class/pwm/pwmchip3/        # 看里面已经导出了哪些 pwmM
```

`chip` = `pwmchip` 后面的数字；`channel` = `pwm` 后面的数字。

> **不确定就两个都写 `-1`。** 那样 fanctl 只用内核已经导出的通道，**绝不会去 export 错脚**
> —— 控制错脚比控制不了更糟（可能把别的外设拧了）。

### 5.2 常用参数

```toml
[fan]
auto = true             # 关掉就只手动
temp_hold_s = 120       # 手动档持有多久，0 = 一直手动
default_pct = 60        # <58℃ 时的转速，也是开机兜底
period_ns = 40000       # 25 kHz，别乱改
pulses_per_rev = 2      # 转速读数整体差 2 倍就改这个
socket_enable = true
socket_group = ""       # 留空 = 只有 root 能连
```

### 5.3 改曲线

```toml
[fan.curve]
points = [[58, 70], [64, 85], [70, 100]]
```

读法：温度 **≥** 台阶温度就套那一档，选满足的最后一档。
都不满足走 `default_pct`。几个现成方案见 `examples/curve-presets.toml`。

写反了也没事（比如温度递增、百分比递减）—— 会自动按温度排序修正。

---

## 6. 出问题怎么查

| 现象 | 原因 | 怎么办 |
|---|---|---|
| `path: null` | PWM 通道映射没对 | 按 §5.1 查 chip/channel，改 `fanctl.toml` |
| 没有 `pwmchip` | 设备树没开 PWM | 启用板子的 PWM overlay（不是 fanctl 的活） |
| `rpm: null`（本该有测速的） | 测速线没插 / 没上拉 / GPIO 错 | 4pin 的 tach 是开漏，裸风扇要自己加 10kΩ 上拉到 3.3V |
| 转速差 2 倍 | `pulses_per_rev` 不对 | 改成 1 或 4 试试 |
| `--set` 报权限错 | 没用 sudo，或 socket 组没配 | 加 sudo；或配 `socket_group` |
| 风扇不转 | PWM 通道对但脚没接对 / 风扇坏 | `sudo fanctl --selftest` 看 rpm；没测速线的只能听声音 |
| 服务起不来 | `journalctl -u fanctl -n 50` | 看具体报错 |
| 温度读不到 | 没有 `thermal_zone` | 会一直按 `default_pct` 跑，不影响散热 |

日志：`sudo journalctl -u fanctl -f`

---

## 7. 卸载

```bash
sudo ./uninstall.sh            # 停服务 + 删文件，保留 /etc/fanctl.toml
sudo ./uninstall.sh --purge    # 连配置一起删
```

卸载前它会先把风扇恢复到 60%，不会因为拔了服务把风扇停在 0。

---

## 8. 目录里都有什么

```
zelux-fan/
├── fanctl                  ← 主程序（Python，单文件，零依赖）
├── fanctl.toml             ← 配置模板（装到 /etc/fanctl.toml）
├── fanctl.service          ← systemd 单元
├── install.sh              ← 一键安装
├── uninstall.sh            ← 卸载
├── README.md               ← 你正在看的
└── examples/
    ├── curve-presets.toml  ← 几套常用温控曲线
    └── socket-call.sh      ← socket 调用示例
```

---

## 9. 给"懂一点"的人：几个设计取舍

* **为什么走 root + socket，而不是直接让程序写 `/sys/class/pwm`？**
  `/sys/class/pwm` 只有 root 能写。业务程序/网页一般以普通用户跑，直接写会 EACCES。
  fanctl 以 root 常驻、对外只开一个只认 JSON 的 socket，攻击面就是一句 `{"op":"set",...}`。
  反过来，如果你**本来就有 root**，可以直接 `fanctl --auto` 前台跑，不开 socket。

* **为什么测速用 sysfs 忙轮询，不用 libgpiod？**
  gpiod 的 line request 是**独占**的：fanctl 一占，你自己的代码就再也读不到那条线，
  而且现象是 `rpm` 悄悄变 `null`，极难查。sysfs 谁都能读，代价只是那 0.4 秒的轮询
  —— 只在有人问转速时才发生。

* **为什么只在档位变化时写 PWM？**
  每次写 sysfs 都要过一次内核。温度惯性比 2 秒慢得多，同一档位反复写没有意义，还占 CPU。

* **为什么退出时不停风扇？**
  主控断电前后那几秒正是最热的时候，把风扇关掉属于帮倒忙。

* **`ProtectKernelTunables=yes` 为什么不能用？**
  它会把整个 `/sys` 变只读，PWM 就写不进去了。单元里只锁 `/usr /boot /etc`
  （`ProtectSystem=full`），`/sys` 保持可写。
