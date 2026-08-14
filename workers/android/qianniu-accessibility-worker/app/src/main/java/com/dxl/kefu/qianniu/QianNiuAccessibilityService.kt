package com.dxl.kefu.qianniu

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.AccessibilityServiceInfo
import android.content.Intent
import android.graphics.Rect
import android.os.Bundle
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.MainScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.withTimeoutOrNull
import java.security.MessageDigest
import java.util.concurrent.CancellationException
import kotlin.coroutines.resume
import kotlin.math.max

private const val TAG = "QNWkr"

private const val PKG_QIANNIU = "com.taobao.qianniu"
private const val INPUT_ID = "com.taobao.qianniu:id/msgcenter_panel_input_edit"
private const val INPUT_ID_NEW = "com.taobao.qianniu:id/qnmessage_chatinput_edt"
private const val TAB_LAYOUT_ID = "com.taobao.qianniu:id/tabLayout"
private const val CHAT_TEXT_ID = "com.taobao.qianniu:id/tv_chat_text"
private const val CHAT_USER_NAME_ID = "com.taobao.qianniu:id/tv_user_name"
private const val CHAT_WRAPPER_ID = "com.taobao.qianniu:id/chat_msg_item_wrapper"
private const val CHAT_FLOW_ID = "com.taobao.qianniu:id/msgflow_recycler"
private const val BUILD_MARKER = "qnwkr-20260627-timeout-sendfix-v2"
private const val SERVICE_ATTITUDE_A11Y_TRIES = 3
private const val SERVICE_ATTITUDE_VERIFY_WAIT_MS = 180L
private const val GUARD_ASSIST_SIGNAL_COOLDOWN_MS = 4000L

private val LIST_IGNORE_TEXT = setOf(
    "在线", "星标", "工作台", "消息", "营销", "头条", "服务",
    "未处理的离线消息", "点击查看近2天未处理的离线消息",
    "[已读]", "[未读]", "未读", "已读", "稍等", "[草稿]",
    "对方正在输入…", "对方正在输入...", "正在输入"
)

private val CHAT_IGNORE_TEXT = setOf(
    "头像", "设置", "返回上一页", "相册", "小额打款", "邀请下单",
    "已读", "对方正在输入…", "对方正在输入...", "正在输入"
)

data class NodeRef(
    val node: AccessibilityNodeInfo,
    val text: String,
    val viewId: String,
    val className: String,
    val clickable: Boolean,
    val bounds: Rect,
)

data class PendingConversation(
    val nickname: String,
    val clickNode: AccessibilityNodeInfo,
    val tapX: Int,
    val y: Int,
    val previewText: String,
    val unreadCount: Int,
    val timeText: String,
    val timeNeedReply: Boolean,
    val timeAgeSec: Int,
)

data class ExtractedMessage(
    val messageType: String,
    val text: String,
    val signature: String,
    val imageBoundsSig: String = "",
    val captureSha1: String = "",
    val top: Int = 0,
    val isSelf: Boolean = false,
    val needsOcr: Boolean = false,
)

data class ChatSessionProgress(
    var idleRounds: Int = 0,
    var initialized: Boolean = false,
    val baselineIgnoredKeys: MutableSet<String> = linkedSetOf(),
    val handledMessageKeys: MutableSet<String> = linkedSetOf(),
)

data class CustomerTurn(
    val startRowIndex: Int,
    val endRowIndex: Int,
    val rows: List<ExtractedMessage>,
    val messageFingerprints: List<String>,
    val top: Int,
    val messageType: String,
    val text: String,
    val imageBoundsSig: String = "",
    val imageCount: Int = 0,
)

data class TurnCandidate(
    val startRowIndex: Int,
    val endRowIndex: Int,
    val seqInEpoch: Int,
    val key: String,
    val fingerprint: String,
    val messageId: String,
    val turn: CustomerTurn,
)

class QianNiuAccessibilityService : AccessibilityService(), CoroutineScope by MainScope() {
    private var tickerJob: Job? = null
    @Volatile
    private var scanningNow: Boolean = false

    // 千牛有时不稳定触发无障碍事件，增加主动扫描兜底（与事件扫描共用同一处理逻辑）。
    private val activeScanIntervalMs: Long = 1200L
    private val chatTargetTimeoutMs: Long = 60_000L
    private val blankChatTimeoutMs: Long = 10_000L
    private val blankRecoverCooldownMs: Long = 5_000L

    private var expectedNickname: String = ""
    private var expectedSetAtMs: Long = 0L
    private var listCursor: Int = 0
    private var listPreviewBootstrapped: Boolean = false
    private var blankChatSinceMs: Long = 0L
    private var lastBlankRecoverAtMs: Long = 0L
    private var lastChatDiagAtMs: Long = 0L
    private var activeChatSessionKey: String = ""
    private var activeChatProgress: ChatSessionProgress? = null
    private var lastGuardAssistSignalAtMs: Long = 0L

    private val previewSeenSig = mutableMapOf<String, String>()
    private val lastReplyTextBySession = mutableMapOf<String, String>()

    override fun onServiceConnected() {
        super.onServiceConnected()
        serviceInfo = serviceInfo.apply {
            eventTypes = AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED or
                AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED or
                AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED
            feedbackType = AccessibilityServiceInfo.FEEDBACK_GENERIC
            flags = AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS or
                AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS or
                AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS
            notificationTimeout = 120
            packageNames = arrayOf(PKG_QIANNIU)
        }
        logi("service connected $BUILD_MARKER")
        startActiveScanTicker()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event == null) return
        val pkg = event.packageName?.toString().orEmpty()
        if (pkg != PKG_QIANNIU) return
        // 单循环为主，事件仅做轻触发，避免并发抢锁卡死。
        triggerOneScan()
    }

    override fun onInterrupt() {
        logi("service interrupted")
    }

    override fun onDestroy() {
        super.onDestroy()
        tickerJob?.cancel()
    }

    private fun triggerOneScan() {
        launch {
            if (scanningNow) return@launch
            scanningNow = true
            try {
                scanOnce()
            } catch (_: CancellationException) {
                // ignore
            } catch (e: Exception) {
                logw("scan error: ${e.javaClass.simpleName}: ${e.message}")
            } finally {
                scanningNow = false
            }
        }
    }

    private fun startActiveScanTicker() {
        tickerJob?.cancel()
        tickerJob = launch {
            var tick = 0
            while (isActive) {
                delay(activeScanIntervalMs)
                if (scanningNow) continue
                scanningNow = true
                try {
                    scanOnce()
                } catch (_: CancellationException) {
                    // 服务停止或协程取消，忽略。
                } catch (e: Exception) {
                    logw("active scan error: ${e.javaClass.simpleName}: ${e.message}")
                } finally {
                    scanningNow = false
                }
                tick += 1
                if (tick % 20 == 0) {
                    logi("ticker alive $BUILD_MARKER")
                }
            }
        }
    }

    private suspend fun scanOnce() {
        val root = rootInActiveWindow ?: return
        val nodes = flatten(root)
        // 白屏恢复只由 guard 负责，worker 不做页面级自愈，避免双边互相放大。
        if (handleWorkbenchPage(nodes)) {
            return
        }
        if (handleBlockingPopupIfAny(nodes)) {
            delay(120)
            return
        }
        if (dismissOfflinePopupIfAny(nodes)) {
            delay(120)
            return
        }
        when {
            isChatPage(nodes) -> handleChat(nodes)
            isListPage(nodes) -> handleList(nodes)
            else -> Unit
        }
    }

    private fun isBlankLikeChatPage(nodes: List<NodeRef>): Boolean {
        if (isChatPage(nodes)) return false
        if (isListPage(nodes)) return false
        val hasChatContainer = nodes.any { ridEq(it.viewId, "com.taobao.qianniu:id/chat_container") }
        val hasBg = nodes.any { ridEq(it.viewId, "com.taobao.qianniu:id/iv_chat_component") }
        val hasTitleBar = nodes.any { it.text == "返回上一页" } && nodes.any { it.text == "设置" }
        val hasMessageSignals = nodes.any {
            ridEq(it.viewId, CHAT_FLOW_ID) || ridEq(it.viewId, CHAT_WRAPPER_ID) || ridEq(it.viewId, CHAT_TEXT_ID)
        }
        return (hasChatContainer || hasBg || hasTitleBar) && !hasMessageSignals
    }

    private fun isWorkbenchPage(nodes: List<NodeRef>): Boolean {
        if (isChatPage(nodes)) return false
        val hasSlidePanel = nodes.any { ridEq(it.viewId, "com.taobao.qianniu:id/slide_panel") }
        val hasTabHost = nodes.any { ridEq(it.viewId, "android:id/tabhost") }
        val hasShopName = nodes.any { ridEq(it.viewId, "com.taobao.qianniu:id/tv_shop_name") }
        val hasWorkbenchMetricText = nodes.any { it.text == "支付金额" || it.text == "访客数" || it.text == "支付子订单数" }
        val hasListHeaderText = nodes.any { it.text == "接待" || it.text == "重要" || it.text == "交易" || it.text == "店铺" }
        val hasOfflineBanner = nodes.any { it.text == "未处理的离线消息" || it.text.contains("未处理的离线消息") }
        if (hasListHeaderText || hasOfflineBanner) return false
        return (hasSlidePanel && hasTabHost) || hasShopName || hasWorkbenchMetricText
    }

    private suspend fun handleWorkbenchPage(nodes: List<NodeRef>): Boolean {
        if (!isWorkbenchPage(nodes)) return false
        val msgTab = nodes
            .filter {
                it.bounds.top >= 1820 &&
                    it.bounds.bottom <= 2030 &&
                    (it.text == "消息" || it.text.contains("消息"))
            }
            .minByOrNull {
                kotlin.math.abs(it.bounds.centerX() - 405) + kotlin.math.abs(it.bounds.centerY() - 1950)
            }
        var ok = false
        if (msgTab != null) {
            ok = clickNode(msgTab.node)
        }
        if (!ok) {
            dispatchGestureTap(405, 1950)
            logw("中控台检测到，消息tab节点点击失败，已坐标兜底")
        } else {
            logi("中控台检测到，已点击消息tab")
        }
        delay(240)
        return true
    }

    private suspend fun handleBlankChatWatchdog(nodes: List<NodeRef>): Boolean {
        if (!isBlankLikeChatPage(nodes)) {
            blankChatSinceMs = 0L
            return false
        }

        val now = System.currentTimeMillis()
        if (blankChatSinceMs == 0L) {
            blankChatSinceMs = now
            logw("检测到疑似聊天白屏，开始计时")
            return true
        }
        val stuckMs = now - blankChatSinceMs
        if (stuckMs < blankChatTimeoutMs) {
            return true
        }
        if (now - lastBlankRecoverAtMs < blankRecoverCooldownMs) {
            return true
        }
        lastBlankRecoverAtMs = now
        blankChatSinceMs = 0L
        recoverBlankChat(stuckMs)
        return true
    }

    private suspend fun recoverBlankChat(stuckMs: Long) {
        logw("聊天页卡住超过${stuckMs}ms，执行自愈")
        performGlobalAction(GLOBAL_ACTION_BACK)
        delay(260)

        val rootAfterBack = rootInActiveWindow
        if (rootAfterBack != null) {
            val nodesAfterBack = flatten(rootAfterBack)
            if (isListPage(nodesAfterBack) || !isBlankLikeChatPage(nodesAfterBack)) {
                logi("白屏自愈：返回后页面恢复")
                return
            }
        }

        val intent = packageManager.getLaunchIntentForPackage(PKG_QIANNIU)
        if (intent != null) {
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
            startActivity(intent)
            logw("白屏自愈：已重启千牛首页")
        } else {
            performGlobalAction(GLOBAL_ACTION_HOME)
            logw("白屏自愈：未取到启动 Intent，已回桌面")
        }
        delay(300)
    }

    private fun isListPage(nodes: List<NodeRef>): Boolean {
        // 聊天页中也会出现大量“昵称样式文本”，必须先排除输入框，避免误判列表。
        if (hasChatInput(nodes)) return false
        // 旧版可用 tabLayout，新版千牛有时没有该 id，需要多信号判定。
        if (nodes.any { ridEq(it.viewId, TAB_LAYOUT_ID) }) return true
        if (nodes.any { ridEq(it.viewId, "com.taobao.qianniu:id/tv_time") }) return true
        if (nodes.any { it.text == "未处理的离线消息" || it.text.contains("未处理的离线消息") }) return true
        val hasListHeaderText = nodes.count { it.text == "接待" || it.text == "重要" || it.text == "交易" || it.text == "店铺" || it.text == "营销" } >= 2
        val hasOnlineFilter = nodes.any { it.text == "在线" } && nodes.any { it.text == "星标" }
        val hasNicknameLike = nodes.any {
            val v = it.text
            v.isNotBlank() &&
                it.bounds.top >= 260 && it.bounds.bottom <= 1750 &&
                it.bounds.left in 150..460 && it.bounds.right <= 560 &&
                !LIST_IGNORE_TEXT.contains(v) &&
                !isTimeLike(v) &&
                !v.all { c -> c.isDigit() }
        }
        return hasNicknameLike && (hasListHeaderText || hasOnlineFilter)
    }

    private fun isChatPage(nodes: List<NodeRef>): Boolean = hasChatInput(nodes)

    private fun handleList(nodes: List<NodeRef>) {
        val directTimeTarget = pickDirectTimeTarget(nodes)
        if (directTimeTarget != null) {
            openListTarget(directTimeTarget, forceByTime = true)
            return
        }

        val visible = extractVisibleConversations(nodes)
        if (visible.isEmpty()) {
            logi("列表无可见会话")
            return
        }
        if (!listPreviewBootstrapped) {
            visible.forEach { conv ->
                if (!conv.timeNeedReply && conv.previewText.isNotBlank()) {
                    previewSeenSig[buildSessionKey(conv.nickname)] = norm(conv.previewText).take(120)
                }
            }
            listPreviewBootstrapped = true
        }

        val target = pickTarget(visible) ?: return
        openListTarget(target, forceByTime = false)
    }

    private fun openListTarget(target: PendingConversation, forceByTime: Boolean) {
        val sessionKey = buildSessionKey(target.nickname)
        val sig = norm(target.previewText).take(120)
        if (sig.isNotBlank()) previewSeenSig[sessionKey] = sig
        expectedNickname = target.nickname
        expectedSetAtMs = System.currentTimeMillis()

        var ok = clickNode(target.clickNode)
        if (!ok) {
            // 列表昵称节点不一定可点，兜底直接点整行的稳定命中区域。
            dispatchGestureTap(target.tapX, target.y)
            ok = true
            logw("列表节点点击失败，坐标兜底: ${target.nickname} x=${target.tapX} y=${target.y}")
        }
        if (!ok) {
            expectedNickname = ""
            return
        }
        if (forceByTime || target.timeNeedReply) {
            logi("时间信号触发: ${target.nickname} time='${target.timeText}' age_sec=${target.timeAgeSec}")
        } else {
            logi("预览触发: ${target.nickname} preview='${target.previewText.take(40)}'")
        }
    }

    private fun pickTarget(visible: List<PendingConversation>): PendingConversation? {
        val timeTargets = visible.filter { it.timeNeedReply }
        if (timeTargets.isNotEmpty()) {
            // 强信号（秒/分钟）必须优先，不能被冷却挡住，否则容易漏回。
            val cands = timeTargets
                .sortedWith(compareByDescending<PendingConversation> { it.timeAgeSec }.thenBy { it.y })
            if (cands.isNotEmpty()) return cands.first()
        }

        val n = visible.size
        val start = if (n <= 0) 0 else listCursor % n
        for (step in 0 until n) {
            val conv = visible[(start + step) % n]
            if (conv.previewText.isBlank() && conv.unreadCount <= 0) continue
            val sessionKey = buildSessionKey(conv.nickname)
            val lastReply = lastReplyTextBySession[sessionKey].orEmpty()
            if (conv.previewText.isNotBlank() && isSelfPreview(conv.previewText, lastReply)) {
                previewSeenSig[sessionKey] = norm(conv.previewText).take(120)
                continue
            }
            val previewSig = norm(conv.previewText).take(120)
            if (previewSig.isNotBlank() && previewSeenSig[sessionKey] == previewSig && conv.unreadCount <= 0) {
                continue
            }
            listCursor = (start + step + 1) % n
            return conv
        }
        return null
    }

    private fun pickDirectTimeTarget(nodes: List<NodeRef>): PendingConversation? {
        val timeHits = nodes.mapNotNull { n ->
            if (!ridEq(n.viewId, "com.taobao.qianniu:id/tv_time")) return@mapNotNull null
            if (n.bounds.top !in 240..1760 || n.bounds.left < 700 || n.bounds.right > 1080) return@mapNotNull null
            val timeText = n.text.trim()
            val (needReply, ageSec) = parseNeedReplyTime(timeText)
            if (!needReply) return@mapNotNull null
            Triple(n, timeText, ageSec)
        }.sortedByDescending { it.third }
        if (timeHits.isEmpty()) return null

        val nicknameNodes = nodes.filter {
            val v = it.text
            v.isNotBlank() &&
                it.bounds.top >= 260 && it.bounds.bottom <= 1750 &&
                it.bounds.left in 120..560 && it.bounds.right <= 760 &&
                !LIST_IGNORE_TEXT.contains(v) && !isTimeLike(v) &&
                !(v.length == 1 && isPrivateChar(v[0])) &&
                v.length <= 48
        }
        if (nicknameNodes.isEmpty()) return null

        for ((timeNode, timeText, ageSec) in timeHits) {
            val nick = nicknameNodes.minByOrNull { kotlin.math.abs(it.bounds.centerY() - timeNode.bounds.centerY()) } ?: continue
            val dy = kotlin.math.abs(nick.bounds.centerY() - timeNode.bounds.centerY())
            if (dy > 260) continue
            val rowClick = resolveListRowClickRef(nodes, nick.bounds.centerY())
            val clickRef = rowClick ?: nick
            return PendingConversation(
                nickname = nick.text,
                clickNode = clickRef.node,
                tapX = computeListRowTapX(clickRef.bounds),
                y = clickRef.bounds.centerY(),
                previewText = "",
                unreadCount = 0,
                timeText = timeText,
                timeNeedReply = true,
                timeAgeSec = ageSec,
            )
        }
        return null
    }

    private suspend fun handleChat(nodes: List<NodeRef>) {
        var nickname = extractChatNickname(nodes)
        if (nickname.isBlank()) nickname = expectedNickname
        val manualMode = expectedNickname.isBlank()
        if (manualMode) {
            // 只处理“列表选中的目标会话”。非目标聊天页直接回列表，避免历史消息误判。
            if (nickname.isNotBlank()) {
                logi("非目标会话，返回列表: $nickname")
            } else {
                logi("非目标会话且未识别昵称，返回列表")
            }
            backToList()
            return
        }

        if (nickname.isBlank()) {
            logw("会话内未识别昵称，放弃本轮，不自动返回")
            expectedNickname = ""
            expectedSetAtMs = 0L
            return
        }

        if (!manualMode && expectedNickname.isNotBlank() && nickname != expectedNickname) {
            logw("打开会话与目标不一致，清空目标不回退: expected='$expectedNickname', actual='$nickname'")
            expectedNickname = ""
            expectedSetAtMs = 0L
            return
        }

        val sessionKey = buildSessionKey(nickname)
        val visibleRowCountNow = countVisibleChatRows(nodes)
        val nowTs = System.currentTimeMillis()
        if (visibleRowCountNow <= 1 || nowTs - lastChatDiagAtMs >= 8000L) {
            lastChatDiagAtMs = nowTs
            val recyclerCount = nodes.count {
                it.className.contains("RecyclerView", ignoreCase = true) && it.bounds.top in 160..1910
            }
            val inputCount = countChatInputs(nodes)
            val tabCount = nodes.count { ridEq(it.viewId, TAB_LAYOUT_ID) }
            val tvTimeCount = nodes.count { ridEq(it.viewId, "com.taobao.qianniu:id/tv_time") }
            logi(
                "[$sessionKey] chat_tree nodes=${nodes.size} rows=$visibleRowCountNow " +
                    "recycler=$recyclerCount input=$inputCount tab=$tabCount tv_time=$tvTimeCount"
            )
        }
        var rows = extractRecentCustomerMessages(nodes, nickname, limit = 80, sessionKey = sessionKey, verboseDiag = true)
        if (rows.isEmpty()) {
            delay(180)
            val root1 = rootInActiveWindow
            if (root1 != null) {
                rows = extractRecentCustomerMessages(
                    flatten(root1),
                    nickname,
                    limit = 80,
                    sessionKey = sessionKey,
                    verboseDiag = true
                )
            }
        }
        if (rows.isEmpty()) {
            // 新版详情页依赖 RecyclerView 行结构，空树时主动触发一次消息区滚动后重试。
            findChatRecyclerNode(nodes)?.performAction(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD)
            delay(180)
            val root2 = rootInActiveWindow
            if (root2 != null) {
                rows = extractRecentCustomerMessages(
                    flatten(root2),
                    nickname,
                    limit = 80,
                    sessionKey = sessionKey,
                    verboseDiag = true
                )
            }
        }
        if (rows.isEmpty()) {
            delay(260)
            val root3 = rootInActiveWindow
            if (root3 != null) {
                rows = extractRecentCustomerMessages(
                    flatten(root3),
                    nickname,
                    limit = 80,
                    sessionKey = sessionKey,
                    verboseDiag = true
                )
            }
        }
        if (rows.isEmpty()) {
            if (!manualMode && System.currentTimeMillis() - expectedSetAtMs > chatTargetTimeoutMs) {
                logw("目标会话建连超时，清空目标: $expectedNickname")
                expectedNickname = ""
                expectedSetAtMs = 0L
                return
            }
            logi("[$sessionKey] 会话提取为空，保留在聊天页等待下轮重试 rows=$visibleRowCountNow")
            return
        }
        logi(
            "[$sessionKey] extracted_rows size=${rows.size}: " +
                rows.joinToString(" || ") {
                    val side = if (it.isSelf) "R" else "L"
                    "${side}@${it.top}/${it.messageType}:'${it.text.take(24)}'"
                }
        )

        if (activeChatSessionKey != sessionKey || activeChatProgress == null) {
            activeChatSessionKey = sessionKey
            val progress = ChatSessionProgress(
                idleRounds = 0,
                initialized = false,
                baselineIgnoredKeys = linkedSetOf(),
                handledMessageKeys = linkedSetOf(),
            )
            activeChatProgress = progress
            logi(
                "[$sessionKey] init_session"
            )
        }
        val progress = activeChatProgress ?: return
        val cfg = WorkerPrefs.load(this)
        val client = DecisionApiClient(cfg)
        val deviceSerial = cfg.deviceSerial.trim()
        val firstLastSelfIndex = rows.indexOfLast { it.isSelf }
        val firstPendingTurns = buildPendingTurnsForSession(
            rows = rows,
            sessionKey = sessionKey,
            progress = progress,
            bootstrapBoundaryIndex = firstLastSelfIndex,
        )
        val previewKeysFirst = firstPendingTurns.map { it.key }
        logi(
            "[$sessionKey] pending_preview size=${previewKeysFirst.size}: " +
                previewKeysFirst.joinToString(",")
        )
        if (previewKeysFirst.isEmpty()) {
            progress.idleRounds += 1
            logi(
                "[$sessionKey] no_pending source=0 idle_rounds=${progress.idleRounds} " +
                    "baseline=${progress.baselineIgnoredKeys.size} handled=${progress.handledMessageKeys.size}"
            )
            if (progress.idleRounds >= 2) {
                backToList()
            }
            return
        }

        delay(350)
        val root2 = rootInActiveWindow ?: run {
            logw("[$sessionKey] pending_verify abort: root_null")
            return
        }
        val nodes2 = flatten(root2)
        if (!isChatPage(nodes2)) {
            logw("[$sessionKey] pending_verify abort: no_longer_chat")
            return
        }
        val nickname2 = extractChatNickname(nodes2).ifBlank { nickname }
        if (norm(nickname2) != norm(nickname)) {
            logw("[$sessionKey] pending_verify abort: chat_switched_to='$nickname2'")
            return
        }
        val rows2 = extractRecentCustomerMessages(
            nodes = nodes2,
            nickname = nickname2,
            limit = 80,
            sessionKey = sessionKey,
            verboseDiag = false,
        )
        if (rows2.isEmpty()) {
            logw("[$sessionKey] pending_verify abort: rows_empty")
            return
        }
        val bootstrapBoundaryIndex = rows2.indexOfLast { it.isSelf }
        val candidates = buildPendingTurnsForSession(
            rows = rows2,
            sessionKey = sessionKey,
            progress = progress,
            bootstrapBoundaryIndex = bootstrapBoundaryIndex,
        )
        val previewKeysSecond = candidates.map { it.key }
        if (previewKeysFirst != previewKeysSecond) {
            logw(
                "[$sessionKey] pending_verify unstable: " +
                    "first=${previewKeysFirst.joinToString(",")} " +
                    "second=${previewKeysSecond.joinToString(",")}"
            )
            return
        }
        ensureSessionBaseline(
            rows = rows2,
            progress = progress,
            bootstrapBoundaryIndex = bootstrapBoundaryIndex,
        )
        val pendingRowIndexes = collectCandidateRowIndexes(candidates)
        val hydratedRows = hydrateRowsWithBridgeOcr(
            rows = rows2,
            sessionKey = sessionKey,
            deviceSerial = deviceSerial,
            client = client,
            allowedRowIndexes = pendingRowIndexes,
        ) ?: return
        val source = buildPendingTurnsForSession(
            rows = hydratedRows,
            sessionKey = sessionKey,
            progress = progress,
            bootstrapBoundaryIndex = bootstrapBoundaryIndex,
        )
        val hydratedKeys = source.map { it.key }
        if (previewKeysSecond != hydratedKeys) {
            logw(
                "[$sessionKey] pending_hydrate unstable: " +
                    "before=${previewKeysSecond.joinToString(",")} " +
                    "after=${hydratedKeys.joinToString(",")}"
            )
            return
        }
        logi(
            "[$sessionKey] pending_source size=${source.size}: " +
                source.joinToString(" || ") {
                    "rows=${it.startRowIndex}-${it.endRowIndex} seq=${it.seqInEpoch} key='${it.key}' " +
                        "mid=${it.messageId.take(12)} fp='${it.fingerprint}' type=${it.turn.messageType} " +
                        "parts=${it.turn.rows.size} text='${it.turn.text.take(40)}'"
                }
        )
        logi(
            "[$sessionKey] session_state initialized=${progress.initialized} " +
                "bootstrap_boundary=$bootstrapBoundaryIndex " +
                "baseline=${progress.baselineIgnoredKeys.size} handled=${progress.handledMessageKeys.size}"
        )
        if (source.isEmpty()) {
            progress.idleRounds += 1
            logi(
                "[$sessionKey] no_pending source=0 idle_rounds=${progress.idleRounds} " +
                    "baseline=${progress.baselineIgnoredKeys.size} handled=${progress.handledMessageKeys.size}"
            )
            if (progress.idleRounds >= 2) {
                backToList()
            }
            return
        }
        expectedSetAtMs = System.currentTimeMillis()
        progress.idleRounds = 0
        val pendingTurns = source
        logi("[$sessionKey] 待处理回合 ${pendingTurns.size} 个，顺序处理")

        for ((idx, cand) in pendingTurns.withIndex()) {
            val turn = cand.turn
            val isImageMsg = turn.messageType == "image"
            val imageSha1 = if (isImageMsg) {
                turn.rows.firstOrNull { it.messageType == "image" }?.captureSha1?.trim().orEmpty()
            } else {
                ""
            }
            logi(
                "[$sessionKey] pending_turn[$idx] seq=${cand.seqInEpoch} mid=${cand.messageId.take(12)} " +
                    "rows=${cand.startRowIndex}-${cand.endRowIndex} key='${cand.key}' type=${turn.messageType} " +
                    "top=${turn.top} parts=${turn.rows.size} images=${turn.imageCount} " +
                    "fp='${cand.fingerprint}' text='${turn.text.take(80)}'"
            )
            val mediaUrl = ""

            val event = IncomingEvent(
                tenantId = cfg.tenantId,
                platform = "taobao",
                storeId = cfg.storeId,
                storeName = cfg.storeName,
                customerId = nickname,
                platformNickname = nickname,
                messageId = cand.messageId,
                messageType = turn.messageType,
                text = turn.text,
                mediaUrl = mediaUrl,
                timestampMs = System.currentTimeMillis(),
                raw = mapOf(
                    "channel_capabilities" to mapOf(
                        "channel" to "worker",
                        "platform" to "taobao",
                        "send_text" to true,
                        "send_image" to false,
                        "send_image_input" to "none",
                    ),
                    "customer_seq" to cand.seqInEpoch,
                    "message_key" to cand.key,
                    "message_fingerprint" to cand.fingerprint,
                    "turn_start_row" to cand.startRowIndex,
                    "turn_end_row" to cand.endRowIndex,
                    "turn_part_count" to turn.rows.size,
                    "turn_image_count" to turn.imageCount,
                    "media_sha1" to imageSha1,
                    "image_bounds" to if (isImageMsg) turn.imageBoundsSig else "",
                    "chat_image_bounds" to "",
                    "image_capture_mode" to if (isImageMsg) "chat_bounds_crop" else "",
                    "image_data_attached" to false,
                    "device_serial" to deviceSerial,
                ),
                role = "customer",
            )

            val decision = runCatching {
                logi("[$sessionKey] decide start, msg='${event.text.take(40)}'")
                withTimeout(65000L) { client.decide(event) }
            }.getOrElse { e ->
                logw("[$sessionKey] decide failed: ${e.javaClass.simpleName}")
                DecisionResult(
                    action = if (cfg.fallbackText.isNotBlank()) "send" else "skip",
                    replyText = cfg.fallbackText,
                    reason = "fallback:${e.javaClass.simpleName}",
                    traceId = "",
                )
            }
            logi("[$sessionKey] 收到消息 type=${event.messageType}, id=${event.messageId}, text='${event.text.take(80)}'")
            logi("[$sessionKey] decision action=${decision.action}, reason=${decision.reason}")

            if (decision.action != "send" || decision.replyText.isBlank()) {
                progress.handledMessageKeys.addAll(cand.turn.messageFingerprints)
                client.ack(event, decision, sentText = "", status = "skipped")
                logi("[$sessionKey] ack status=skipped key='${cand.key}' id=${event.messageId}")
                if (idx < pendingTurns.lastIndex) delay(120)
                continue
            }

            val sentOk = sendText(decision.replyText)
            if (sentOk) {
                progress.handledMessageKeys.addAll(cand.turn.messageFingerprints)
                lastReplyTextBySession[sessionKey] = decision.replyText
                expectedSetAtMs = System.currentTimeMillis()
                client.ack(event, decision, sentText = decision.replyText, status = "sent")
                logi("[$sessionKey] 已发送回复")
                logi("[$sessionKey] ack status=sent key='${cand.key}' id=${event.messageId}")
            } else {
                client.ack(event, decision, sentText = "", status = "failed")
                logw("[$sessionKey] 发送失败，等待下轮重试")
                logw("[$sessionKey] ack status=failed key='${cand.key}' id=${event.messageId}")
                break
            }
            if (idx < pendingTurns.lastIndex) delay(120)
        }
    }

    private suspend fun sendText(text: String): Boolean {
        val root = rootInActiveWindow ?: return false
        val nodes = flatten(root)
        val input = findChatInputNode(nodes) ?: return false
        val inputRect = Rect()
        input.getBoundsInScreen(inputRect)
        if (!clickNode(input)) {
            dispatchGestureTap(inputRect.centerX(), inputRect.centerY())
        }
        delay(120)
        val nodesAfterFocus = flatten(rootInActiveWindow ?: root)
        val inputForSet = findChatInputNode(nodesAfterFocus) ?: input
        val setArgs = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        }
        val setOk = inputForSet.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, setArgs)
        if (!setOk) return false

        // 设置文本后再抓一次节点，避免“发送”按钮延迟出现导致漏点。
        delay(180)
        var nodes2 = flatten(rootInActiveWindow ?: root)
        if (handleBlockingPopupIfAny(nodes2)) {
            delay(180)
            nodes2 = flatten(rootInActiveWindow ?: root)
        }
        val beforeRightSigs = collectRightBubbleSignatures(nodes2)
        val sendNode = nodes2.firstOrNull {
            val t = it.text
            (t == "发送" || it.viewId.contains("send", ignoreCase = true)) &&
                (it.clickable || it.className.contains("Button", ignoreCase = true))
        }?.node

        val clickOk = if (sendNode != null) {
            clickNode(sendNode)
        } else {
            // 兜底：部分千牛版本的发送按钮不暴露文本，点击输入栏同一行最右侧控件。
            val inRect = Rect()
            input.getBoundsInScreen(inRect)
            val rowRightNode = nodes2
                .filter {
                    it.clickable &&
                        it.bounds.centerY() in (inRect.top - 80)..(inRect.bottom + 80) &&
                        it.bounds.left > inRect.right
                }
                .maxByOrNull { it.bounds.right }
            if (rowRightNode != null) {
                logi("发送按钮文本未暴露，兜底点击右侧控件 viewId=${rowRightNode.viewId} bounds=${rowRightNode.bounds}")
                clickNode(rowRightNode.node)
            } else {
                logi("发送按钮文本未暴露，兜底点击右侧发送区域 x=995 y=${inRect.centerY()}")
                dispatchGestureTap(995, inRect.centerY())
            }
            true
        }
        if (!clickOk) return false

        delay(220)
        val afterSend = flatten(rootInActiveWindow ?: root)
        if (handleBlockingPopupIfAny(afterSend)) {
            delay(200)
        }
        val finalNodes = flatten(rootInActiveWindow ?: root)
        val afterRightSigs = collectRightBubbleSignatures(finalNodes)
        val hasNewRightBubble = afterRightSigs.any { it !in beforeRightSigs }
        val hasRightReplyText = hasRightBubbleContainingText(finalNodes, text)
        val inputNow = findChatInputNode(finalNodes)?.text?.toString()?.trim().orEmpty()
        if (inputNow.isBlank() || norm(inputNow) != norm(text)) return true
        if (hasNewRightBubble && hasRightReplyText) {
            logi("发送后检测到新右侧气泡，判定发送成功")
            return true
        }
        logw("发送后输入框仍为原文，疑似被弹窗阻塞")
        return false
    }

    private suspend fun backToList() {
        clearChatDraftIfAny()
        for (idx in 0 until 2) {
            performGlobalAction(GLOBAL_ACTION_BACK)
            delay(180)
            val root = rootInActiveWindow ?: break
            val nodes = flatten(root)
            if (isListPage(nodes) || !isChatPage(nodes)) {
                break
            }
            if (idx == 1) {
                logw("返回列表失败，仍在聊天页")
            }
        }
        resetChatSessionProgress()
        expectedNickname = ""
        expectedSetAtMs = 0L
    }

    private fun resetChatSessionProgress() {
        activeChatSessionKey = ""
        activeChatProgress = null
    }

    private fun clearChatDraftIfAny() {
        val root = rootInActiveWindow ?: return
        val nodes = flatten(root)
        val input = findChatInputNode(nodes) ?: return
        val txt = (input.text ?: "").toString().trim()
        if (txt.isBlank()) return
        val args = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, "")
        }
        if (input.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)) {
            logi("检测到未发送草稿，返回前已清空输入框")
        }
    }

    private suspend fun handleBlockingPopupIfAny(nodes: List<NodeRef>): Boolean {
        if (handleServiceAttitudeDialogIfAny(nodes)) return true
        if (handleResendConfirmDialogIfAny(nodes)) return true
        if (handleInstallUpdateDialogIfAny(nodes)) return true
        if (handleOfficialWarningDialogIfAny(nodes)) return true
        return false
    }

    private suspend fun handleServiceAttitudeDialogIfAny(nodes: List<NodeRef>): Boolean {
        val hasTitle = nodes.any { it.text.contains("服务态度提醒") }
        if (!hasTitle) return false

        repeat(SERVICE_ATTITUDE_A11Y_TRIES) {
            val rootNow = rootInActiveWindow ?: return@repeat
            val nodesNow = flatten(rootNow)
            if (nodesNow.none { n -> n.text.contains("服务态度提醒") }) {
                logi("检测到服务态度提醒弹窗，已关闭")
                return true
            }
            if (clickDialogButtonByTexts(nodesNow, listOf("继续发送", "确认继续", "仍要发送", "继续"))) {
                delay(SERVICE_ATTITUDE_VERIFY_WAIT_MS)
                val rootAfter = rootInActiveWindow
                val nodesAfter = rootAfter?.let { flatten(it) } ?: emptyList()
                if (nodesAfter.none { n -> n.text.contains("服务态度提醒") }) {
                    logi("检测到服务态度提醒弹窗，已点继续发送")
                    return true
                }
            }
        }

        signalGuardAssist("service_attitude")
        logw("检测到服务态度提醒弹窗，无障碍点击未生效，已请求guard协助")
        return true
    }

    private fun signalGuardAssist(kind: String) {
        val now = System.currentTimeMillis()
        if (now - lastGuardAssistSignalAtMs < GUARD_ASSIST_SIGNAL_COOLDOWN_MS) {
            return
        }
        lastGuardAssistSignalAtMs = now
        logw("GUARD_ASSIST:$kind")
    }

    private fun handleResendConfirmDialogIfAny(nodes: List<NodeRef>): Boolean {
        val hasTitle = nodes.any { it.text.contains("您要重新发送这条消息吗？") }
        if (!hasTitle) return false
        if (clickDialogButtonByTexts(nodes, listOf("确定", "继续发送"))) {
            logi("检测到重发确认弹窗，已点确定")
            return true
        }
        dispatchGestureTap(875, 1087)
        logw("检测到重发确认弹窗，确定按钮节点点击失败，已坐标点确定兜底")
        return true
    }

    private fun handleInstallUpdateDialogIfAny(nodes: List<NodeRef>): Boolean {
        val hasInstallDialog = nodes.any {
            it.text == "安装" ||
                it.text.contains("是否安装") ||
                it.text.contains("新版本的千牛")
        }
        val hasUpdateDialog = nodes.any {
            it.text.contains("快来更新千牛") ||
                it.text.contains("更新包大小")
        } && nodes.any { it.text == "立即下载" || it.text.contains("立即下载") }

        if (!hasInstallDialog && !hasUpdateDialog) return false
        val cancelLabels = if (hasUpdateDialog) {
            listOf("拒绝", "取消", "稍后再说", "以后再说")
        } else {
            listOf("取消", "稍后再说", "以后再说")
        }
        if (clickDialogButtonByTexts(nodes, cancelLabels)) {
            logi("检测到千牛更新安装弹窗，已点取消/拒绝")
            return true
        }
        dispatchGestureTap(235, 1880)
        logw("检测到千牛更新安装弹窗，取消/拒绝按钮节点点击失败，已坐标点左侧按钮兜底")
        return true
    }

    private fun handleOfficialWarningDialogIfAny(nodes: List<NodeRef>): Boolean {
        val hasWarn = nodes.any { it.text.contains("淘宝官方预警") || it.text.contains("风险预警") }
        if (!hasWarn) return false
        if (clickDialogButtonByTexts(nodes, listOf("我知道了", "知道了", "关闭"))) {
            logi("检测到官方预警弹窗，已确认关闭")
            return true
        }
        return false
    }

    private fun clickDialogButtonByTexts(nodes: List<NodeRef>, labels: List<String>): Boolean {
        for (label in labels) {
            val byText = nodes
                .filter {
                val t = it.text.trim()
                t.isNotBlank() &&
                    (t == label || t.contains(label))
                }
                .maxByOrNull { it.bounds.top }
            if (byText != null) {
                if ((byText.clickable || byText.className.contains("Button", ignoreCase = true)) && clickNode(byText.node)) {
                    return true
                }
                dispatchGestureTap(byText.bounds.centerX(), byText.bounds.centerY())
                return true
            }
        }
        return false
    }

    private fun dismissOfflinePopupIfAny(nodes: List<NodeRef>): Boolean {
        val hasPopupTitle = nodes.any { it.text == "未处理离线消息" || it.text.contains("未处理离线消息") }
        if (!hasPopupTitle) return false

        // 优先点弹层右上角 X 图标（文本常为私有区字符，例如 \ue670）
        val closeNode = nodes.firstOrNull {
            it.bounds.left >= 930 && it.bounds.top in 250..420 &&
                (it.text == "\ue670" || it.text == "\ue5cd" || it.text == "×")
        }
        if (closeNode != null && clickNode(closeNode.node)) {
            logi("检测到未处理离线消息弹层，已关闭")
            return true
        }

        // 兜底：按坐标点击右上角关闭位
        val root = rootInActiveWindow ?: return false
        val r = Rect()
        root.getBoundsInScreen(r)
        val x = r.right - 45
        val y = 330
        dispatchGestureTap(x, y)
        logi("检测到未处理离线消息弹层，坐标兜底关闭")
        return true
    }

    private fun dispatchGestureTap(x: Int, y: Int): Boolean {
        return runCatching {
            val p = android.graphics.Path().apply { moveTo(x.toFloat(), y.toFloat()) }
            val stroke = android.accessibilityservice.GestureDescription.StrokeDescription(p, 0, 80)
            val gesture = android.accessibilityservice.GestureDescription.Builder().addStroke(stroke).build()
            dispatchGesture(gesture, null, null)
        }.getOrElse { false }
    }

    private suspend fun dispatchGestureTapAwait(x: Int, y: Int): Boolean {
        return withTimeoutOrNull(900L) {
            suspendCancellableCoroutine { cont ->
                val p = android.graphics.Path().apply { moveTo(x.toFloat(), y.toFloat()) }
                val stroke = android.accessibilityservice.GestureDescription.StrokeDescription(p, 0, 80)
                val gesture = android.accessibilityservice.GestureDescription.Builder().addStroke(stroke).build()
                val ok = runCatching {
                    dispatchGesture(
                        gesture,
                        object : GestureResultCallback() {
                            override fun onCompleted(gestureDescription: android.accessibilityservice.GestureDescription?) {
                                if (cont.isActive) cont.resume(true)
                            }

                            override fun onCancelled(gestureDescription: android.accessibilityservice.GestureDescription?) {
                                if (cont.isActive) cont.resume(false)
                            }
                        },
                        null,
                    )
                }.getOrElse { false }
                if (!ok && cont.isActive) cont.resume(false)
            }
        } ?: false
    }

    private fun extractVisibleConversations(nodes: List<NodeRef>): List<PendingConversation> {
        val nicknameNodes = nodes.filter {
            val v = it.text
            v.isNotBlank() &&
                it.bounds.top >= 260 && it.bounds.bottom <= 1750 &&
                it.bounds.left in 150..460 && it.bounds.right <= 560 &&
                !LIST_IGNORE_TEXT.contains(v) && !isTimeLike(v) &&
                !(v.length == 1 && isPrivateChar(v[0])) &&
                !(v.all { c -> c.isDigit() } && it.bounds.right <= 240) &&
                v.length <= 48
        }.sortedBy { it.bounds.centerY() }

        val previewNodes = nodes.filter {
            val v = it.text
            v.isNotBlank() &&
                it.bounds.top >= 260 && it.bounds.bottom <= 1750 &&
                it.bounds.left in 160..950 &&
                !LIST_IGNORE_TEXT.contains(v) &&
                !isTimeLike(v) &&
                !v.all { c -> c.isDigit() } &&
                !isOnlyPunc(v) &&
                v.length <= 120 &&
                !v.any { c -> isPrivateChar(c) }
        }

        val timeNodes = nodes.filter {
            val v = it.text
            v.isNotBlank() &&
                it.bounds.top >= 240 && it.bounds.bottom <= 1760 &&
                it.bounds.left >= 700 && it.bounds.right <= 1080 &&
                isTimeLike(v)
        }

        val out = mutableListOf<PendingConversation>()
        for (i in nicknameNodes.indices) {
            val nickNode = nicknameNodes[i]
            val nickname = nickNode.text
            val rowTop = max(260, nickNode.bounds.top - 30)
            val nextY = if (i + 1 < nicknameNodes.size) nicknameNodes[i + 1].bounds.top else 1800
            val rowBottom = minOf(1760, max(nickNode.bounds.bottom + 30, nextY - 20))

            val previews = previewNodes.filter {
                it.bounds.centerY() in rowTop..rowBottom && it.text != nickname
            }.sortedWith(compareBy<NodeRef> { it.bounds.centerY() }.thenBy { it.bounds.left })
            val previewText = previews.lastOrNull()?.text.orEmpty()
            val unreadCount = nodes.filter {
                val v = it.text.trim()
                v.matches(Regex("^\\d{1,3}$")) &&
                    it.bounds.centerY() in rowTop..rowBottom &&
                    it.bounds.left <= 180 &&
                    it.bounds.top <= nickNode.bounds.top + 40
            }.maxOfOrNull { it.text.trim().toIntOrNull() ?: 0 } ?: 0

            val times = timeNodes.filter { it.bounds.centerY() in rowTop..rowBottom }
                .sortedWith(compareBy<NodeRef> { kotlin.math.abs(it.bounds.centerY() - nickNode.bounds.centerY()) }.thenByDescending { it.bounds.left })
            val timeText = times.firstOrNull()?.text.orEmpty()
            val (timeNeedReply, ageSec) = parseNeedReplyTime(timeText)
            val rowClick = resolveListRowClickRef(nodes, nickNode.bounds.centerY())
            val clickRef = rowClick ?: nickNode

            out += PendingConversation(
                nickname = nickname,
                clickNode = clickRef.node,
                tapX = computeListRowTapX(clickRef.bounds),
                y = clickRef.bounds.centerY(),
                previewText = previewText,
                unreadCount = unreadCount,
                timeText = timeText,
                timeNeedReply = timeNeedReply,
                timeAgeSec = ageSec,
            )
        }

        val dedup = linkedMapOf<String, PendingConversation>()
        out.forEach { if (!dedup.containsKey(it.nickname)) dedup[it.nickname] = it }
        return dedup.values.toList()
    }

    private fun resolveListRowClickRef(nodes: List<NodeRef>, centerY: Int): NodeRef? {
        return nodes.filter {
            it.clickable &&
                it.bounds.top <= centerY &&
                it.bounds.bottom >= centerY &&
                it.bounds.left <= 80 &&
                it.bounds.right >= 900 &&
                it.bounds.height() >= 120
        }.minByOrNull {
            kotlin.math.abs(it.bounds.centerY() - centerY) + kotlin.math.abs(it.bounds.left - 35)
        }
    }

    private fun computeListRowTapX(bounds: Rect): Int {
        return if (bounds.width() >= 700) {
            (bounds.left + 245).coerceIn(180, 420)
        } else {
            bounds.centerX().coerceIn(180, 420)
        }
    }

    private fun extractChatNickname(nodes: List<NodeRef>): String {
        val nickNode = nodes.filter {
            ridEq(it.viewId, CHAT_USER_NAME_ID) &&
                it.text.isNotBlank() &&
                it.bounds.bottom <= 260
        }.minByOrNull { it.bounds.top }
        return nickNode?.text.orEmpty()
    }

    private fun extractRecentCustomerMessages(
        nodes: List<NodeRef>,
        nickname: String,
        limit: Int,
        sessionKey: String,
        verboseDiag: Boolean = false,
    ): List<ExtractedMessage> {
        val unusedLimit = limit
        if (unusedLimit < 0) return emptyList()
        val rowRoots = collectVisibleChatRows(nodes)
        if (rowRoots.isEmpty()) {
            if (verboseDiag) {
                val recyclerCount = nodes.count {
                    it.className.contains("RecyclerView", ignoreCase = true) && it.bounds.top in 160..1910
                }
                val inputCount = countChatInputs(nodes)
                val tabCount = nodes.count { ridEq(it.viewId, TAB_LAYOUT_ID) }
                val tvTimeCount = nodes.count { ridEq(it.viewId, "com.taobao.qianniu:id/tv_time") }
                logw(
                    "[$sessionKey] row_diag rows=0 nodes=${nodes.size} " +
                        "recycler=$recyclerCount input=$inputCount tab=$tabCount tv_time=$tvTimeCount"
                )
            }
            return emptyList()
        }

        val extractedRows = mutableListOf<Triple<Int, Int, ExtractedMessage>>()
        val rowDiags = mutableListOf<String>()

        for ((idx, rowRoot) in rowRoots.withIndex()) {
            val rowBounds = nodeBounds(rowRoot)
            val extracted = extractRecyclerRowMessage(rowRoot, nickname)
            if (extracted == null) {
                if (verboseDiag || rowRoots.size <= 2) {
                    rowDiags += "#$idx side=unknown type=skip bounds=${boundsSig(rowBounds)}"
                }
                continue
            }
            val side = if (extracted.isSelf) "right" else "left"
            extractedRows += Triple(rowBounds.centerY(), idx, extracted)
            if (verboseDiag || rowRoots.size <= 2) {
                rowDiags += "#$idx side=$side type=${extracted.messageType} bounds=${boundsSig(rowBounds)}"
            }
        }
        if ((verboseDiag || rowRoots.size <= 2) && rowDiags.isNotEmpty()) {
            logi("[$sessionKey] row_diag rows=${rowRoots.size} detail=${rowDiags.joinToString(" || ")}")
        }
        return extractedRows.sortedBy { it.first }.map { it.third }.takeLast(limit)
    }

    private fun countVisibleChatRows(nodes: List<NodeRef>): Int = collectVisibleChatRows(nodes).size

    private suspend fun hydrateRowsWithBridgeOcr(
        rows: List<ExtractedMessage>,
        sessionKey: String,
        deviceSerial: String,
        client: DecisionApiClient,
        allowedRowIndexes: Set<Int>? = null,
    ): List<ExtractedMessage>? {
        val items = mutableListOf<WorkerOcrItem>()
        for ((idx, row) in rows.withIndex()) {
            if (allowedRowIndexes != null && idx !in allowedRowIndexes) continue
            if (row.isSelf) continue
            if (!row.needsOcr) continue
            val bounds = row.imageBoundsSig.trim()
            if (bounds.isBlank()) continue
            items += WorkerOcrItem(itemId = "row_$idx", bounds = bounds)
        }
        if (items.isEmpty()) return rows
        if (deviceSerial.isBlank()) {
            logw("[$sessionKey] ocr skipped: device_serial_empty")
            return null
        }

        val captureMap = runCatching {
            withTimeout(45000L) {
                client.captureOcrBatch(deviceSerial, items)
            }.associateBy { it.itemId }
        }.getOrElse { e ->
            logw("[$sessionKey] ocr batch failed: ${e.javaClass.simpleName}")
            return null
        }

        var nonEmptyCount = 0
        var hashedCount = 0
        val hydrated = rows.mapIndexed { idx, row ->
            if (row.isSelf) return@mapIndexed row
            if (!row.needsOcr) return@mapIndexed row
            val result = captureMap["row_$idx"]
            val ocrText = result?.ocrText.orEmpty().trim()
            val sha1 = result?.sha1.orEmpty().trim()
            if (ocrText.isNotBlank()) nonEmptyCount += 1
            if (sha1.isNotBlank()) hashedCount += 1
            row.copy(
                text = ocrText,
                captureSha1 = sha1,
            )
        }
        logi("[$sessionKey] ocr_batch items=${items.size} non_empty=$nonEmptyCount hashed=$hashedCount")
        return hydrated
    }

    private fun collectCandidateRowIndexes(candidates: List<TurnCandidate>): Set<Int> {
        val out = linkedSetOf<Int>()
        for (candidate in candidates) {
            if (candidate.startRowIndex > candidate.endRowIndex) continue
            for (idx in candidate.startRowIndex..candidate.endRowIndex) {
                out += idx
            }
        }
        return out
    }

    private fun buildPendingTurnsForSession(
        rows: List<ExtractedMessage>,
        sessionKey: String,
        progress: ChatSessionProgress,
        bootstrapBoundaryIndex: Int,
    ): List<TurnCandidate> {
        val startAfterIndex = if (progress.initialized) -1 else bootstrapBoundaryIndex
        val ignoredMessageKeys = if (progress.initialized) {
            linkedSetOf<String>().apply {
                addAll(progress.baselineIgnoredKeys)
                addAll(progress.handledMessageKeys)
            }
        } else {
            emptySet()
        }
        val turns = buildPendingCustomerTurns(
            rows = rows,
            startAfterIndex = startAfterIndex,
            ignoredMessageKeys = ignoredMessageKeys,
        )
        val out = mutableListOf<TurnCandidate>()
        val baseCounts = mutableMapOf<String, Int>()
        var seqInEpoch = 0
        for (turn in turns) {
            val fingerprint = buildStableTurnFingerprint(turn)
            val base = "L|$fingerprint"
            val occurrence = baseCounts[base] ?: 0
            baseCounts[base] = occurrence + 1
            val key = "$base#$occurrence"
            val messageId = md5Hex("$sessionKey|$key").take(24)
            out += TurnCandidate(
                startRowIndex = turn.startRowIndex,
                endRowIndex = turn.endRowIndex,
                seqInEpoch = seqInEpoch,
                key = key,
                fingerprint = fingerprint,
                messageId = messageId,
                turn = turn,
            )
            seqInEpoch += 1
        }
        return out
    }

    private fun ensureSessionBaseline(
        rows: List<ExtractedMessage>,
        progress: ChatSessionProgress,
        bootstrapBoundaryIndex: Int,
    ) {
        if (progress.initialized) return
        progress.baselineIgnoredKeys.clear()
        progress.baselineIgnoredKeys.addAll(
            collectMessageFingerprints(
                rows = rows,
                endInclusive = bootstrapBoundaryIndex,
            )
        )
        progress.initialized = true
    }

    private fun collectMessageFingerprints(
        rows: List<ExtractedMessage>,
        endInclusive: Int,
    ): Set<String> {
        if (endInclusive < 0) return emptySet()
        val out = linkedSetOf<String>()
        val capped = minOf(endInclusive, rows.lastIndex)
        for (idx in 0..capped) {
            val msg = rows[idx]
            if (msg.isSelf) continue
            val fingerprint = buildStableMessageFingerprint(msg) ?: continue
            out += fingerprint
        }
        return out
    }

    private fun collectVisibleChatRows(nodes: List<NodeRef>): List<AccessibilityNodeInfo> {
        val recycler = findChatRecyclerNode(nodes) ?: return emptyList()
        val out = mutableListOf<AccessibilityNodeInfo>()
        for (i in 0 until recycler.childCount) {
            val child = recycler.getChild(i) ?: continue
            val bounds = nodeBounds(child)
            if (bounds.bottom < 190 || bounds.top > 1885) continue
            if (bounds.height() < 40) continue
            out += child
        }
        return out
    }

    private fun findChatRecyclerNode(nodes: List<NodeRef>): AccessibilityNodeInfo? {
        val direct = nodes.firstOrNull {
            ridEq(it.viewId, CHAT_FLOW_ID) && it.bounds.top in 160..1910
        }?.node
        if (direct != null) return direct
        return nodes
            .filter {
                it.className.contains("RecyclerView", ignoreCase = true) &&
                    it.bounds.top in 160..1910 &&
                    it.bounds.height() >= 320
            }
            .maxByOrNull { it.bounds.height() }
            ?.node
    }

    private fun nodeBounds(node: AccessibilityNodeInfo): Rect {
        val rect = Rect()
        node.getBoundsInScreen(rect)
        return rect
    }

    private fun extractRecyclerRowMessage(
        rowRoot: AccessibilityNodeInfo,
        nickname: String,
    ): ExtractedMessage? {
        val rowBounds = nodeBounds(rowRoot)
        val rowNodes = flatten(rowRoot)
        if (rowNodes.isEmpty()) return null

        val leftAvatar = rowNodes.any {
            looksLikeChatAvatar(it) && it.bounds.centerX() <= rowBounds.centerX() - 140
        }
        val rightAvatar = rowNodes.any {
            looksLikeChatAvatar(it) && it.bounds.centerX() >= rowBounds.centerX() + 140
        }
        val nicknameHit = rowNodes.any {
            it.className.contains("TextView", ignoreCase = true) &&
                norm(it.text) == norm(nickname) &&
                it.bounds.left < rowBounds.centerX()
        }
        val senderLabelRight = rowNodes.any {
            it.className.contains("TextView", ignoreCase = true) &&
                it.text.isNotBlank() &&
                !isTimeLike(it.text) &&
                !isReadReceiptText(it.text) &&
                !CHAT_IGNORE_TEXT.contains(it.text) &&
                norm(it.text) != norm(nickname) &&
                it.bounds.left > rowBounds.centerX()
        }
        val side = when {
            nicknameHit || (leftAvatar && !rightAvatar) -> "left"
            rightAvatar || senderLabelRight -> "right"
            else -> "unknown"
        }
        if (side == "unknown") return null

        val bubbleRect = pickRowBubbleRect(rowNodes, rowBounds)
        val rowSigBounds = bubbleRect ?: rowBounds
        val rowSig = stableRowBoundsSig(rowSigBounds)
        val sendTimeText = extractRowSendTime(rowNodes, rowBounds)
        val rowSigBase = "side=$side|sendtime=${if (sendTimeText.isBlank()) "-" else sendTimeText}|bounds=$rowSig"

        if (side == "right") {
            return ExtractedMessage(
                messageType = "self",
                text = "",
                signature = "$rowSigBase|type=self",
                imageBoundsSig = bubbleRect?.let(::boundsSig).orEmpty(),
                top = rowBounds.centerY(),
                isSelf = true,
            )
        }

        val textParts = collectBubbleTextParts(rowNodes, bubbleRect, nickname)
        val hasVoiceToken = rowNodes.any {
            bubbleRect != null &&
                inside(it.bounds, bubbleRect) &&
                (containsPrivateChar(it.text) || isAudioDurationText(it.text))
        }
        val imageNodes = rowNodes.filter {
            looksLikeRowMessageImage(it, rowBounds, bubbleRect)
        }
        val captureBoundsSig = bubbleRect?.let(::boundsSig).orEmpty()
        val textBoundsSig = captureBoundsSig.ifBlank { boundsSig(rowBounds) }

        if (hasVoiceToken) {
            return ExtractedMessage(
                messageType = "text",
                text = "[语音消息]",
                signature = "$rowSigBase|type=audio",
                imageBoundsSig = captureBoundsSig,
                top = rowBounds.centerY(),
                isSelf = false,
            )
        }
        if (textParts.isNotEmpty()) {
            val text = textParts.joinToString("\n").trim()
            return ExtractedMessage(
                messageType = "text",
                text = text,
                signature = "$rowSigBase|type=text|text=${md5Hex(norm(text)).take(12)}",
                imageBoundsSig = "",
                top = rowBounds.centerY(),
                isSelf = false,
            )
        }
        if (imageNodes.isNotEmpty()) {
            val bubbleHeight = bubbleRect?.height() ?: 0
            if (bubbleHeight >= 500) {
                return ExtractedMessage(
                    messageType = "image",
                    text = "[图片]",
                    signature = "$rowSigBase|type=image|img=$captureBoundsSig",
                    imageBoundsSig = captureBoundsSig,
                    top = rowBounds.centerY(),
                    isSelf = false,
                )
            }
            if (bubbleHeight in 380..490) {
                return ExtractedMessage(
                    messageType = "text",
                    text = "",
                    signature = "$rowSigBase|type=textcard",
                    imageBoundsSig = captureBoundsSig,
                    top = rowBounds.centerY(),
                    isSelf = false,
                    needsOcr = true,
                )
            }
            logi("drop_nonstandard_image_row bounds=$captureBoundsSig h=$bubbleHeight")
            return null
        }
        if (captureBoundsSig.isNotBlank()) {
            return ExtractedMessage(
                messageType = "text",
                text = "",
                signature = "$rowSigBase|type=text",
                imageBoundsSig = textBoundsSig,
                top = rowBounds.centerY(),
                isSelf = false,
                needsOcr = true,
            )
        }
        return null
    }

    private fun pickRowBubbleRect(rowNodes: List<NodeRef>, rowBounds: Rect): Rect? {
        val rowArea = max(1, rowBounds.width()) * max(1, rowBounds.height())
        val candidates = rowNodes.filter {
            it.bounds != rowBounds &&
                it.bounds.top >= rowBounds.top + 45 &&
                it.bounds.bottom <= rowBounds.bottom + 4 &&
                it.bounds.left >= 120 &&
                it.bounds.right <= rowBounds.right - 20 &&
                it.bounds.width() >= 80 &&
                it.bounds.height() >= 56 &&
                max(1, it.bounds.width()) * max(1, it.bounds.height()) < rowArea * 8 / 10 &&
                !isTimeLike(it.text) &&
                !isReadReceiptText(it.text) &&
                !looksLikeChatAvatar(it)
        }
        return candidates.maxByOrNull { max(1, it.bounds.width()) * max(1, it.bounds.height()) }?.bounds
    }

    private fun collectBubbleTextParts(
        rowNodes: List<NodeRef>,
        bubbleRect: Rect?,
        nickname: String,
    ): List<String> {
        val out = mutableListOf<String>()
        val scope = bubbleRect
        val textNodes = rowNodes
            .filter {
                it.text.isNotBlank() &&
                    !containsPrivateChar(it.text) &&
                    !isAudioDurationText(it.text) &&
                    !isTimeLike(it.text) &&
                    !isReadReceiptText(it.text) &&
                    !CHAT_IGNORE_TEXT.contains(it.text) &&
                    !LIST_IGNORE_TEXT.contains(it.text) &&
                    norm(it.text) != norm(nickname) &&
                    (scope == null || inside(it.bounds, scope))
            }
            .sortedWith(compareBy<NodeRef> { it.bounds.top }.thenBy { it.bounds.left })
        for (node in textNodes) {
            val value = node.text.trim()
            if (value.isBlank() || out.contains(value)) continue
            out += value
        }
        return out
    }

    private fun extractRowSendTime(rowNodes: List<NodeRef>, rowBounds: Rect): String {
        val rowTime = rowNodes.filter {
            isTimeLike(it.text) &&
                it.bounds.centerX() in 260..860 &&
                it.bounds.centerY() in rowBounds.top..rowBounds.bottom
        }.sortedByDescending { it.bounds.centerY() }
        return rowTime.firstOrNull()?.text?.trim().orEmpty()
    }

    private fun isReadReceiptText(v0: String): Boolean {
        val v = v0.trim()
        return v == "已读" || v == "未读"
    }

    private fun isAudioDurationText(v0: String): Boolean {
        val v = v0.trim()
        return v.matches(Regex("^\\d+\"$"))
    }

    private fun containsPrivateChar(v: String): Boolean = v.any { isPrivateChar(it) }

    private fun looksLikeRowMessageImage(
        node: NodeRef,
        rowBounds: Rect,
        bubbleRect: Rect?,
    ): Boolean {
        if (bubbleRect == null) return false
        if (looksLikeChatAvatar(node)) return false
        if (!node.className.contains("ImageView", ignoreCase = true)) return false
        if (!inside(node.bounds, bubbleRect)) return false
        val w = node.bounds.width()
        val h = node.bounds.height()
        if (w < 120 || h < 120) return false
        if (w > rowBounds.width() - 30 && h > rowBounds.height() - 30) return false
        return true
    }

    private fun buildPendingCustomerTurns(
        rows: List<ExtractedMessage>,
        startAfterIndex: Int,
        ignoredMessageKeys: Set<String>,
    ): List<CustomerTurn> {
        val out = mutableListOf<CustomerTurn>()
        val bucket = mutableListOf<Triple<Int, ExtractedMessage, String>>()
        val seenMessageKeys = ignoredMessageKeys.toMutableSet()

        fun flushTurn() {
            if (bucket.isEmpty()) return
            out += buildCustomerTurn(bucket)
            bucket.clear()
        }

        for ((idx, msg) in rows.withIndex()) {
            if (idx <= startAfterIndex) continue
            if (msg.isSelf) {
                flushTurn()
                continue
            }
            val messageFingerprint = buildStableMessageFingerprint(msg)
            if (messageFingerprint == null) {
                logw("skip row without content fingerprint: sig='${msg.signature.take(80)}'")
                continue
            }
            if (!seenMessageKeys.add(messageFingerprint)) continue
            if (msg.messageType == "image") {
                // Keep each customer image as its own turn. Merging consecutive image rows
                // causes duplicate/mixed replies when the visible crop changes after a send.
                flushTurn()
                out += buildCustomerTurn(listOf(Triple(idx, msg, messageFingerprint)))
                continue
            }
            bucket += Triple(idx, msg, messageFingerprint)
        }
        flushTurn()
        return out
    }

    private fun buildCustomerTurn(parts: List<Triple<Int, ExtractedMessage, String>>): CustomerTurn {
        val rows = parts.map { it.second }
        val fingerprints = parts.map { it.third }
        val startRowIndex = parts.first().first
        val endRowIndex = parts.last().first
        val textParts = mutableListOf<String>()
        var imageCount = 0
        val captureRects = mutableListOf<Rect>()
        for (msg in rows) {
            when (msg.messageType) {
                "image" -> {
                    parseBoundsSig(msg.imageBoundsSig)?.let { captureRects += it }
                    imageCount += 1
                }
                else -> {
                    val value = msg.text.trim()
                    if (value.isNotBlank()) textParts += value
                }
            }
        }
        val mergedText = textParts.joinToString("\n").trim()
        val captureBoundsSig = unionRects(captureRects)?.let(::boundsSig).orEmpty()
        val turnType = if (imageCount > 0) "image" else "text"
        return CustomerTurn(
            startRowIndex = startRowIndex,
            endRowIndex = endRowIndex,
            rows = rows,
            messageFingerprints = fingerprints,
            top = rows.first().top,
            messageType = turnType,
            text = mergedText,
            imageBoundsSig = captureBoundsSig,
            imageCount = max(imageCount, captureRects.size),
        )
    }

    private fun buildStableTurnFingerprint(turn: CustomerTurn): String {
        val joined = turn.messageFingerprints.joinToString(">>")
        return "TURN|${md5Hex("$joined|parts=${turn.rows.size}|images=${turn.imageCount}").take(16)}"
    }

    private fun collectRightBubbleSignatures(nodes: List<NodeRef>): Set<String> {
        val wrappers = nodes.filter {
            ridEq(it.viewId, CHAT_WRAPPER_ID) && it.bounds.top in 160..1910
        }.sortedBy { it.bounds.centerY() }
        if (wrappers.isEmpty()) return emptySet()
        val out = linkedSetOf<String>()
        for (wrapper in wrappers) {
            val side = guessWrapperSide(wrapper, nodes)
            if (side != "right") continue
            val texts = nodes.filter { ridEq(it.viewId, CHAT_TEXT_ID) && inside(it.bounds, wrapper.bounds) }
                .sortedWith(compareBy<NodeRef> { it.bounds.top }.thenBy { it.bounds.left })
                .map { it.text.trim() }
                .filter { it.isNotBlank() && !isTimeLike(it) && !CHAT_IGNORE_TEXT.contains(it) }
            if (texts.isEmpty()) continue
            val merged = texts.joinToString(" | ").trim()
            if (merged.isBlank()) continue
            out += "${boundsSig(wrapper.bounds)}:${md5Hex(norm(merged)).take(8)}"
        }
        return out
    }

    private fun hasRightBubbleContainingText(nodes: List<NodeRef>, text: String): Boolean {
        val target = norm(text)
        if (target.isBlank()) return false
        val wrappers = nodes.filter {
            ridEq(it.viewId, CHAT_WRAPPER_ID) && it.bounds.top in 160..1910
        }.sortedBy { it.bounds.centerY() }
        for (wrapper in wrappers) {
            val side = guessWrapperSide(wrapper, nodes)
            if (side != "right") continue
            val texts = nodes.filter { ridEq(it.viewId, CHAT_TEXT_ID) && inside(it.bounds, wrapper.bounds) }
                .sortedWith(compareBy<NodeRef> { it.bounds.top }.thenBy { it.bounds.left })
                .map { norm(it.text.trim()) }
                .filter { it.isNotBlank() }
            if (texts.any { it.contains(target) || target.contains(it) }) {
                return true
            }
        }
        return false
    }

    private fun guessWrapperSide(
        wrapper: NodeRef,
        nodes: List<NodeRef>,
    ): String {
        val pair = resolveWrapperAvatarAndContent(wrapper, nodes) ?: return "unknown"
        val avatarCx = pair.first.bounds.centerX()
        val contentCx = pair.second.bounds.centerX()
        if (avatarCx < contentCx) return "left"
        if (avatarCx > contentCx) return "right"
        return "unknown"
    }

    private fun resolveWrapperAvatarAndContent(
        wrapper: NodeRef,
        nodes: List<NodeRef>,
    ): Pair<NodeRef, NodeRef>? {
        val avatars = nodes.filter {
            inside(it.bounds, wrapper.bounds) && looksLikeChatAvatar(it)
        }
        val contents = nodes.filter {
            inside(it.bounds, wrapper.bounds) && ridEq(it.viewId, "com.taobao.qianniu:id/tv_chatcontent")
        }
        if (avatars.isEmpty() || contents.isEmpty()) return null

        var bestAvatar: NodeRef? = null
        var bestContent: NodeRef? = null
        var bestDist = Int.MAX_VALUE
        for (avatar in avatars) {
            for (content in contents) {
                val dist = kotlin.math.abs(avatar.bounds.centerY() - content.bounds.top)
                if (dist < bestDist) {
                    bestDist = dist
                    bestAvatar = avatar
                    bestContent = content
                }
            }
        }
        if (bestAvatar == null || bestContent == null) return null
        return bestAvatar to bestContent
    }

    private fun looksLikeChatAvatar(node: NodeRef): Boolean {
        if (!node.className.contains("ImageView", ignoreCase = true)) return false
        val w = node.bounds.width()
        val h = node.bounds.height()
        if (w !in 70..220 || h !in 70..220) return false
        if (node.viewId.lowercase().contains("iv_content_image")) return false
        return true
    }

    private fun flatten(root: AccessibilityNodeInfo): List<NodeRef> {
        val out = ArrayList<NodeRef>(300)
        fun walk(node: AccessibilityNodeInfo?) {
            if (node == null) return
            val r = Rect()
            node.getBoundsInScreen(r)
            val rawText = node.text?.toString().orEmpty().trim()
            val rawDesc = node.contentDescription?.toString().orEmpty().trim()
            val textUsable = rawText.isNotBlank() && rawText.any { !it.isWhitespace() && !isPrivateChar(it) }
            val t = if (textUsable) rawText else rawDesc
            out += NodeRef(
                node = node,
                text = t,
                viewId = node.viewIdResourceName.orEmpty(),
                className = node.className?.toString().orEmpty(),
                clickable = node.isClickable,
                bounds = r,
            )
            for (i in 0 until node.childCount) {
                walk(node.getChild(i))
            }
        }
        walk(root)
        return out
    }

    private fun clickNode(node: AccessibilityNodeInfo?): Boolean {
        if (node == null) return false
        var curr: AccessibilityNodeInfo? = node
        repeat(6) {
            if (curr == null) return@repeat
            if (curr!!.isClickable) {
                if (curr!!.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                    return true
                }
            }
            curr = curr?.parent
        }
        return false
    }

    private fun buildSessionKey(nickname: String): String {
        val cfg = WorkerPrefs.load(this)
        return "taobao:${cfg.storeId}:$nickname"
    }

    private fun buildStableMessageFingerprint(msg: ExtractedMessage): String? {
        return when (msg.messageType) {
            "image" -> {
                val sha1 = msg.captureSha1.trim()
                if (sha1.isNotBlank()) {
                    "I|${sha1.take(12)}"
                } else {
                    "I|${md5Hex(msg.signature).take(12)}"
                }
            }
            "text" -> {
                if (msg.needsOcr) {
                    return "O|${md5Hex(msg.signature).take(12)}"
                }
                val normalized = norm(msg.text)
                if (normalized.isBlank()) return null
                "T|${md5Hex(normalized).take(12)}"
            }
            else -> {
                val prefix = msg.messageType.uppercase().take(1).ifBlank { "U" }
                "$prefix|${md5Hex(msg.signature).take(12)}"
            }
        }
    }

    private fun ridEq(actual: String, expected: String): Boolean {
        if (actual == expected) return true
        return actual.endsWith(expected)
    }

    private fun isChatInputId(viewId: String): Boolean =
        ridEq(viewId, INPUT_ID) || ridEq(viewId, INPUT_ID_NEW)

    private fun hasChatInput(nodes: List<NodeRef>): Boolean =
        nodes.any { isChatInputId(it.viewId) }

    private fun countChatInputs(nodes: List<NodeRef>): Int =
        nodes.count { isChatInputId(it.viewId) }

    private fun findChatInputNode(nodes: List<NodeRef>): AccessibilityNodeInfo? =
        nodes.firstOrNull { isChatInputId(it.viewId) }?.node

    private fun norm(v: String): String = v.replace("\\s+".toRegex(), "")

    private fun isSelfPreview(previewText: String, lastReplyText: String): Boolean {
        val p = norm(previewText)
        val r = norm(lastReplyText)
        if (p.isBlank() || r.isBlank()) return false
        if (p.any { isPrivateChar(it) }) return false
        return p == r
    }

    private fun isPrivateChar(c: Char): Boolean = c.code in 0xE000..0xF8FF

    private fun isOnlyPunc(v: String): Boolean = v.matches(Regex("^[\\[\\]（）()\\-—_·•:：.，,。!！?？~～]+$"))

    private fun isTimeLike(v0: String): Boolean {
        val v = v0.trim()
        if (v.isBlank()) return false
        return v.matches(Regex("^\\d{1,2}:\\d{2}$")) ||
            v.matches(Regex("^(今天|昨天)\\s+\\d{1,2}:\\d{2}$")) ||
            v.matches(Regex("^星期[一二三四五六日天]\\s+\\d{1,2}:\\d{2}$")) ||
            v.matches(Regex("^周[一二三四五六日天]\\s+\\d{1,2}:\\d{2}$")) ||
            v.matches(Regex("^\\d{1,2}月\\d{1,2}日\\s+\\d{1,2}:\\d{2}$")) ||
            v.matches(Regex("^\\d+\\s*秒$")) ||
            v.matches(Regex("^\\d+\\s*分钟$")) ||
            v.matches(Regex("^\\d+\\s*小时$")) ||
            v.matches(Regex("^\\d+\\s*分$")) ||
            v == "刚刚" || v.endsWith("分钟前") || v.endsWith("小时前")
    }

    private fun parseNeedReplyTime(v0: String): Pair<Boolean, Int> {
        val v = v0.trim()
        if (v == "刚刚") return true to 1
        Regex("^(\\d+)\\s*秒$").matchEntire(v)?.let {
            return true to max(1, it.groupValues[1].toInt())
        }
        Regex("^(\\d+)\\s*分钟$").matchEntire(v)?.let {
            return true to max(1, it.groupValues[1].toInt() * 60)
        }
        Regex("^(\\d+)\\s*小时$").matchEntire(v)?.let {
            return true to max(1, it.groupValues[1].toInt() * 3600)
        }
        Regex("^(\\d+)\\s*分$").matchEntire(v)?.let {
            return true to max(1, it.groupValues[1].toInt() * 60)
        }
        return false to 0
    }

    private fun boundsSig(r: Rect): String = "${r.left},${r.top},${r.right},${r.bottom}"

    private fun unionRects(rects: List<Rect>): Rect? {
        if (rects.isEmpty()) return null
        var left = rects.first().left
        var top = rects.first().top
        var right = rects.first().right
        var bottom = rects.first().bottom
        for (rect in rects.drop(1)) {
            left = minOf(left, rect.left)
            top = minOf(top, rect.top)
            right = maxOf(right, rect.right)
            bottom = maxOf(bottom, rect.bottom)
        }
        return Rect(left, top, right, bottom)
    }

    private fun stableRowBoundsSig(r: Rect): String {
        val w = r.width()
        val h = r.height()
        return "${r.left},${r.right},$w,$h"
    }

    private fun parseBoundsSig(sig: String): Rect? {
        val p = sig.split(',')
        if (p.size != 4) return null
        val l = p[0].trim().toIntOrNull() ?: return null
        val t = p[1].trim().toIntOrNull() ?: return null
        val r = p[2].trim().toIntOrNull() ?: return null
        val b = p[3].trim().toIntOrNull() ?: return null
        if (r <= l || b <= t) return null
        return Rect(l, t, r, b)
    }

    private fun inside(inner: Rect, outer: Rect): Boolean {
        return inner.left >= outer.left && inner.right <= outer.right &&
            inner.top >= outer.top && inner.bottom <= outer.bottom
    }

    private fun md5Hex(input: String): String {
        return md5Hex(input.toByteArray(Charsets.UTF_8))
    }

    private fun md5Hex(input: ByteArray): String {
        val md = MessageDigest.getInstance("MD5")
        val bytes = md.digest(input)
        return bytes.joinToString(separator = "") { "%02x".format(it) }
    }

    private fun logi(msg: String) = Log.i(TAG, msg)
    private fun logw(msg: String) = Log.w(TAG, msg)
}
