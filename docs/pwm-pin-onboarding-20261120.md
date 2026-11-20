# Studio 风扇 PWM 物理引脚首次配置

更新时间：2026-11-20

## 2026-11-21 用户真机验证反馈

用户确认 Studio 添加实例成功，并确认 PWM 滑块实际调速、独立 FG 测速及 RPM 反馈完全跑通。此前用户也确认实例禁用、启用、删除正常。以上为用户提供的现场验收反馈，非本地自动测试或本 agent 直接操作设备的结果。当前手动控制核心链路可阶段收口；重启后的实例恢复、自动温控曲线、异常断连恢复尚未收到明确验证结论。

### 首次 export 的零周期初始化与 FG 冲突

现场创建报 enable=0 EINVAL，随后资源释放同样失败。此前 apply/release 都无条件先写 enable=0；新 export 通道可能 period=0，停止写入同样可能触发内核无效 PWM 状态检查。现仅在明确读到 period=0 且 enable=0 时跳过重复停止，apply 先设置合法周期再 duty/enable；release 对此已停用未初始化通道不写无效状态，继续按既有所有权规则释放。无法读取状态或已启用时不跳过停止，不吞掉其他写入错误。现场零周期状态尚需确认；不把所有 EINVAL 都视为相同原因。

截图同时将 pin21 PWM12_M1 与 FG GPIO149 配为同一物理脚，必须纠正；新增清单校验，拒绝任一实例 FG 与已登记 PWM pin21/GPIO149 或 pin24/GPIO150 重叠（包括跨实例）。本轮 fanctl 53 项测试通过，包括零周期严格模拟、释放和冲突先于硬件访问。需要更新设备 fanctl 并重启 zelux-fand，再刷新清单确认此前失败实例/残留资源状态；不应重复创建或手动 unexport 未核实归属的资源。测试不证明真实 PWM 输出。

### 板卡识别兼容修复

### Studio 下拉框实时刷新修复

后续现场发现手动刷新后在 overlay_required 与未知之间切换。根因是 setup 响应被写入 `st.fan_pins`，但 app.js 的 WebSocket 完整快照 `store.set('st', data)` 不包含此字段，每次推送都将其清除。现将按需查询的 setup 状态与实时快照分开保存于风扇页生命周期内；向导、预览/回滚检查与实例编辑统一读取该状态，离开页面清除，重新进入重新查询。查询使用递增请求号，忽略旧请求和卸载后返回；刷新失败明确显示失败并禁用配置，不保留旧 ready 冒充成功。新增回归模拟 20 次不含 fan_pins 的完整设备快照、手动刷新后推送、乱序查询返回，全部通过。只更新 Studio 静态资源并强制刷新；无需修改设备树或重新安装清单。未做现场浏览器验证。

现场在 overlay_required 状态下无法稳定选择主控 PWM 引脚。前端 paint 每次状态通知都 replaceChildren 重建原生 select 的 option；实时推送期间会打断展开的菜单。已将配置向导和实例编辑器的选项更新改为内容对比，只在 value/label 或选项数量变化时替换，并保留有效选择。待配置引脚仍可选，创建实例仍要求 runtime ready。本次仅需更新 Studio 静态资源并强制刷新网页，不需重新安装 overlay 或为此前端修复重启设备。DOM 回归包含连续 20 次全新快照的 option 节点稳定性和选择后按钮可用性；实际浏览器展开行为仍需现场确认。

setup helper 与 fanctl 不再仅依赖 model 文本包含 `Radxa Zero 3W`：旧 Armbian 报告 `Radxa ZERO 3`，现根据根 compatible 的明确 allowlist 与 RK3566 身份识别，仍拒绝 3E 和未知板卡。fanctl 对不支持板卡的 mapped pins 全部标记 ready=false，避免仅控制器同名绕过门控。需配套更新设备端 `zelux-pwm-setupd` 与 `fanctl` 并重启各自服务，然后刷新 Studio；无需因本修复更改引脚/实例配置。详见 device-image 的 `docs/fan-pwm-first-enable.md` 修复记录。

## 目标闭环

Studio 只让用户选择主控板的物理引脚（当前档案：Radxa Zero 3W pin 21 / PWM12-M1、pin 24 / PWM13-M1），不要求用户填写 pwmchip 编号，也不要求手工编辑 /boot/armbianEnv.txt。

1. Studio 读取设备端 zelux-pwm-setupd 的板卡/Armbian/kernel 档案状态，逐引脚显示 overlay、运行设备树 pinmux、PWM 控制器和重启状态。
2. 用户选定引脚后，Studio 请求预览；Agent 不接受路径或 shell 命令，只转发 profile pin ID。预览给出启动配置 diff 和一次性 120 秒 token。
3. 用户明确确认后，受限 root helper 重新校验 token、板卡/OS/kernel、overlay manifest hash 和当前文件内容，备份启动文件，再原子更新 user_overlays。
4. Studio 明确提示用户重启设备。重启后重新读取 helper 状态；只有 live device tree 中 controller pinctrl phandle 与目标 pin group 一致、controller status=okay 且 PWM controller 已注册，pin 才标记 ready。
5. 创建或重新启用实例时，Agent 再向 setup helper 查询设备端状态，检查 pwm_pin 对应的 chip/channel。fanctl 再执行实际 PWM runtime 生命周期，避免 UI 状态或人为传入的 chip/channel 绕过门控。

当前配置操作不会远程重启设备，也不会在未知 profile 上猜测。未知或冲突环境 fail closed，可通过 helper 的 allowlisted rollback 操作删除本项目添加的 overlay ID；启动文件备份保存在 /var/lib/zelux-pwm-setup/。

## 硬件控制职责与所有权

- zelux-pwm-setupd：只拥有开机 overlay 配置流程及其备份/回滚；不读写 PWM duty，不 export/unexport PWM。
- Device Agent / Studio：传递逻辑 pin ID、呈现状态与确认用户意图；不获得 root 文件写权限。
- fanctl：唯一 PWM/FG 运行时控制者。启动/创建/启用时按需 export；只将自己 export 的 PWM 记入持久 ownership ledger（/var/lib/zelux-fan/pwm-owned.json）；禁用/删除/失败回滚时先停止，再仅 unexport 自己记账的通道。用户预先 export 的通道只停止输出，不会被擅自 unexport。
- inventory 拒绝重复使用同一个 PWM channel，避免一个实例释放另一个实例仍在使用的资源。

## 受限系统档案

只有 Radxa Zero 3、Armbian overlay hook 可识别、内核匹配当前 vendor-rk35xx 6.1.x 受支持规则，并且镜像中的 .dtbo 与 SHA-256 manifest 相符时，才提供自动 overlay 操作。实际板卡/镜像组合未完成真机重启和示波器验证前，不应把软件档案识别误称为硬件验收通过。

设备树 overlay 来源固定到 Radxa overlays 仓库 commit bba8c5214481f607066fe2c227d1e9942daf19e2，源码位于 zelux-device-image/device-tree/overlays/；构建产物安装在 /boot/overlay-user/。不从 Studio 页面下载任意 overlay，也不接受用户传入文件路径、DTBO 内容或命令。

## Socket/API 边界

- Studio → Device Agent：fan.op pwm_setup_status | pwm_setup_preview | pwm_setup_apply。
- Agent → driver：只接受上述枚举操作、逻辑 pin_id、token 和布尔确认。
- driver → root helper：/run/zelux/pwm-setup.sock，每连接一行 JSON；协议 API version 1，响应字段 pwm_setup / preview / result。
- Agent 在 create、enabled update、enable 操作之前调用 status 校验 ready 与 chip/channel 一致。失败不触碰 fanctl 实例。
- fanctl socket API 不因 setup helper 扩展获得 boot 配置写能力；它只处理风扇运行时和资源所有权。

## 构建、部署与自动化验证

- zelux-device-image/build-image.sh、build-from-armbian.sh 在构建环境编译 DT overlay 并写入 image boot 分区及 manifest。
- install.sh 会检查 dtc 并构建/安装 overlay；部署包应确保构建时带有 device-tree-compiler。没有 dtc 时安装明确失败，不会留下界面可配置但 overlay 不存在的假成功。
- 自动测试覆盖启动文件预览/确认/备份/回滚、token 与 allowlist、重复配置冲突、Agent 引脚 ready/mapping 门控、fanctl export/删除 ownership、Studio 配置向导 DOM。
- fake sysfs 测试证明程序资源生命周期，不证明实际 PWM 控制器编号、pinmux 电气复用、风扇波形或冷却效果。目标设备还要验证重启前后状态和 PWM/FG 实测。

## 未由本地自动测试证明的项目

本次没有连接或部署设备。仍需测试 Radxa Zero 3W 实际设备上的 pin21/pin24 controller OF-node 是否与 overlay 档案吻合、Armbian U-Boot overlay hook 是否加载、pinctrl phandle 是否生效、周期/极性/频率与风扇波形是否正确，以及 helper 备份回滚在断电/启动失败场景中的效果。
