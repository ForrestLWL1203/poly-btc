const ERROR_TEXT = {
  secure_context_required: "仅允许通过 HTTPS 或本机安全上下文录入私钥",
  credential_worker_not_provisioned: "VPS 凭据解密服务尚未配置",
  credential_verification_failed: "验证失败：请核对主钱包、Agent 地址、私钥以及 Hyperliquid 官方授权",
  collection_worker_not_provisioned: "QuickNode 凭据服务尚未配置",
  quicknode_endpoint_invalid: "Endpoint 无效：仅接受 QuickNode 的 HTTPS quiknode.pro 地址",
  quicknode_endpoint_not_configured: "尚未配置 QuickNode Endpoint",
  quicknode_endpoint_file_invalid: "QuickNode Endpoint 文件状态异常",
  quicknode_invalid_response: "QuickNode 返回的数据格式无效",
  quicknode_unavailable: "QuickNode 暂时不可用，请稍后重试",
  quicknode_verification_failed: "QuickNode 验证失败，请核对 Endpoint 与套餐状态",
  quicknode_http_401: "QuickNode 凭据无效，请重新复制 Endpoint",
  quicknode_http_403: "QuickNode 套餐无权访问 Hyperliquid Info API",
  quicknode_http_404: "QuickNode Endpoint 路径无效",
  quicknode_http_429: "QuickNode 当前已限速，请稍后重试",
  quicknode_not_verified: "请先到账户信息保存并验证 QuickNode Endpoint",
  collection_source_locked: "采集运行中不能切换数据源；如需强制切换，请先终止本轮",
  mainnet_credential_not_configured: "请先到账户信息配置实盘 Agent",
  mainnet_credential_not_verified: "请先到账户信息完成实盘 Agent 验证",
  mainnet_credential_in_use: "实盘会话运行期间不能替换或删除 Agent；请先排空停止",
  observer_must_be_stopped: "切换 Paper/实盘模式前，请先使用右上角按钮停止当前跟单",
  live_preflight_not_passed: "实盘启动检查尚未通过",
  live_confirmation_phrase_mismatch: "实盘启动确认失败",
  live_exposure_prevents_paper_switch: "仍有真实仓位或订单，不能切回 Paper",
  OBSERVER_MUST_BE_STOPPED: "请先使用右上角按钮停止当前跟单",
  SYSTEM_CLOCK_NOT_SYNCHRONIZED: "VPS 系统时间尚未同步",
  STRATEGY_REVISION_INVALID: "当前策略版本不可执行",
  NO_EXECUTABLE_CORE_TARGETS: "当前没有可执行的 Core 钱包",
  MARKET_METADATA_INCOMPLETE: "实盘所需市场元数据不完整",
  WEBSOCKET_UNAVAILABLE: "Hyperliquid WebSocket 暂不可用",
  AGENT_MISMATCH: "Agent 未授权给当前主钱包",
  UNSUPPORTED_ACCOUNT_MODE: "Hyperliquid 账户不是 Unified 模式",
  ACCOUNT_NOT_CLEAN: "首次启动实盘前，账户必须没有仓位和挂单",
  NO_AVAILABLE_COLLATERAL: "Hyperliquid 账户没有可用 USDC",
  NO_EXECUTABLE_CAPACITY: "可用资金不足以形成最小合法订单",
};

export const friendlyExecutionError = error => {
  const code = String(error?.message || error || "操作失败");
  return ERROR_TEXT[code] || code;
};

export async function activateLiveAndStart(api) {
  const check = await api.cmdAndWait("execution_preflight", {}, 90000);
  if (!check.ok) throw new Error(check.code || "live_preflight_not_passed");
  await api.cmdAndWait("activate_live", {
    preflightId: check.preflightId,
    confirmationPhrase: "启动实盘",
  });
  return api.cmdAndWait("observer_start", {}, 90000);
}
