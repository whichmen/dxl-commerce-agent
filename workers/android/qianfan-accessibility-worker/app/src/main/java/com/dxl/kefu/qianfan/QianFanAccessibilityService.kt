package com.dxl.kefu.qianfan

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.AccessibilityServiceInfo
import android.graphics.Rect
import android.os.Bundle
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.MainScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeout
import java.security.MessageDigest
import kotlin.math.abs
import kotlin.math.max

private const val TAG = "QFWkr"
private const val PKG_QIANFAN = "com.xingin.eva"
private const val BUILD_MARKER = "qfwkr-20260506a"
private const val LIST_CLICK_COOLDOWN_MS = 1800L
private const val POST_SEND_STABILIZE_MS = 1_000L
private const val PRE_CONSUME_STABLE_DELAY_MS = 500L
private const val CHAT_ENTER_STABILIZE_MS = 2_000L
private const val IMAGE_PREVIEW_ENTER_WAIT_MS = 2_000L
private const val IMAGE_RETRY_COOLDOWN_MS = 9_000L
private const val IMAGE_PREVIEW_OPEN_MAX_FAILS = 2
private const val NAV_FIX_COOLDOWN_MS = 1_800L
private const val FULLSCREEN_IMAGE_PREVIEW_STUCK_MS = 30_000L
private const val FULLSCREEN_IMAGE_PREVIEW_BACK_COOLDOWN_MS = 10_000L

data class NodeRef(
    val text: String,
    val viewId: String,
    val clazz: String,
    val bounds: Rect,
    val clickable: Boolean,
    val enabled: Boolean,
    val pkg: String,
    val raw: AccessibilityNodeInfo?,
)

data class PendingConversation(
    val nickname: String,
    val previewText: String,
    val waitText: String,
    val waitNeedReply: Boolean,
    val waitAgeSec: Int,
    val unreadCount: Int,
    val y: Int,
    val clickNode: NodeRef,
)

data class ExtractedMessage(
    val text: String,
    val sig: String,
    val messageType: String,
    val imageBoundsSig: String,
)

data class ChatRow(
    val top: Int,
    val text: String,
    val isSelf: Boolean,
    val sig: String,
    val messageType: String,
    val imageBoundsSig: String,
)

data class ChatSessionProgress(
    val initialIgnoreKeys: MutableSet<String> = linkedSetOf(),
    val handledKeys: MutableSet<String> = linkedSetOf(),
    val keyMessageIds: MutableMap<String, String> = mutableMapOf(),
    val imagePreviewOpenFailCounts: MutableMap<String, Int> = mutableMapOf(),
    var nextMessageSeq: Long = 0L,
    val sessionSeedMs: Long = 0L,
    var idleRounds: Int = 0,
)

data class ChatCandidate(
    val rowIndex: Int,
    val row: ChatRow,
    val key: String,
)

data class RowStructure(
    val avatarBranch: NodeRef?,
    val contentBranch: NodeRef?,
    val side: String,
)

private data class AvatarRowCandidate(
    val rowRef: NodeRef,
    val structure: RowStructure,
    val depth: Int,
)

class QianFanAccessibilityService : AccessibilityService(), CoroutineScope by MainScope() {
    private var tickerJob: Job? = null
    private var scanningNow = false
    private val activeScanIntervalMs = 1300L
    private val chatTargetTimeoutMs = 60_000L

    private var expectedNickname: String = ""
    private var expectedSetAtMs: Long = 0L
    private var tick = 0
    private var lastListClickAtMs: Long = 0L
    private var lastListClickNickNorm: String = ""
    private var lastNavFixAtMs: Long = 0L
    private var activeChatSessionKey: String = ""
    private var activeChatProgress: ChatSessionProgress? = null
    private var chatFrameSeq: Long = 0L
    private var postSendStableUntilMs: Long = 0L
    private var chatEnteredAtMs: Long = 0L
    private var fullscreenImagePreviewSinceMs: Long = 0L
    private var lastFullscreenImagePreviewBackAtMs: Long = 0L
    private val imageRetryNotBeforeByKey = mutableMapOf<String, Long>()

    private val previewSeenSig = mutableMapOf<String, String>()

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
            packageNames = arrayOf(PKG_QIANFAN)
        }
        logi("service connected $BUILD_MARKER")
        startActiveScanTicker()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event == null) return
        val pkg = event.packageName?.toString().orEmpty()
        if (pkg != PKG_QIANFAN) return
        triggerOneScan()
    }

    override fun onInterrupt() {
        logi("service interrupted")
    }

    override fun onDestroy() {
        super.onDestroy()
        tickerJob?.cancel()
    }

    private fun startActiveScanTicker() {
        tickerJob?.cancel()
        tickerJob = launch {
            while (isActive) {
                delay(activeScanIntervalMs)
                if (scanningNow) continue
                scanningNow = true
                try {
                    scanOnce()
                } catch (_: CancellationException) {
                    // ignore
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

    private suspend fun scanOnce() {
        val root = rootInActiveWindow ?: return
        val pkg = root.packageName?.toString().orEmpty()
        if (pkg != PKG_QIANFAN) return
        val nodes = flatten(root)
        if (dismissServiceSummaryPopupIfAny(nodes)) {
            delay(120)
            return
        }
        if (dismissInstallPopupIfAny(nodes)) {
            delay(120)
            return
        }
        val chatPage = isChatPage(nodes)
        val listPage = if (chatPage) false else isListPage(nodes)
        if (handleFullscreenImagePreviewIfNeeded(nodes, chatPage, listPage)) {
            return
        }
        when {
            chatPage -> handleChat(nodes)
            listPage -> handleList(nodes)
            else -> ensureCustomerListPage(nodes)
        }
    }

    private fun isListPage(nodes: List<NodeRef>): Boolean {
        return isBottomMessagePage(nodes) &&
            isCustomerReceptionHeader(nodes) &&
            hasSessionTabs(nodes)
    }

    private fun isChatPage(nodes: List<NodeRef>): Boolean {
        val hasInput = findChatInputNode(nodes) != null
        if (!hasInput) return false
        val hasScroll = findChatScrollNode(nodes) != null
        if (!hasScroll) return false
        val hasHeaderNick = extractChatNickname(nodes).isNotBlank()
        val hasGuestTag = nodes.any { it.text == "老客" || it.text == "新客" }
        return hasHeaderNick || hasGuestTag || nodes.any { it.text == "发送" }
    }

    private suspend fun handleFullscreenImagePreviewIfNeeded(
        nodes: List<NodeRef>,
        chatPage: Boolean,
        listPage: Boolean,
    ): Boolean {
        if (chatPage || listPage || !looksLikeFullscreenImagePreview(nodes)) {
            fullscreenImagePreviewSinceMs = 0L
            return false
        }

        val now = System.currentTimeMillis()
        if (fullscreenImagePreviewSinceMs <= 0L) {
            fullscreenImagePreviewSinceMs = now
            logw("检测到疑似全屏图片预览，开始计时")
            return true
        }

        val elapsedMs = now - fullscreenImagePreviewSinceMs
        if (elapsedMs >= FULLSCREEN_IMAGE_PREVIEW_STUCK_MS &&
            now - lastFullscreenImagePreviewBackAtMs >= FULLSCREEN_IMAGE_PREVIEW_BACK_COOLDOWN_MS
        ) {
            lastFullscreenImagePreviewBackAtMs = now
            fullscreenImagePreviewSinceMs = 0L
            dispatchGestureTap(540, 1060)
            logw("疑似全屏图片预览停留${elapsedMs}ms，已点图退出")
            delay(300)
        }
        return true
    }

    private fun looksLikeFullscreenImagePreview(nodes: List<NodeRef>): Boolean {
        val visible = nodes.filter { isVisibleNode(it) }
        if (visible.isEmpty()) return false
        if (visible.any { it.clazz.endsWith("EditText") }) return false

        val textNodes = visible.filter { it.text.trim().isNotBlank() }
        if (textNodes.size > 4) return false
        val pageTexts = textNodes.map { it.text.trim() }.toSet()
        val normalPageTexts = setOf("消息", "客服接待", "当前会话", "全部会话", "收藏会话", "发送")
        if (pageTexts.any { it in normalPageTexts }) return false

        val left = visible.minOfOrNull { it.bounds.left } ?: return false
        val top = visible.minOfOrNull { it.bounds.top } ?: return false
        val right = visible.maxOfOrNull { it.bounds.right } ?: return false
        val bottom = visible.maxOfOrNull { it.bounds.bottom } ?: return false
        val screenWidth = max(1, right - left)
        val screenHeight = max(1, bottom - top)

        return visible.any {
            it.clazz.endsWith("ImageView") &&
                it.bounds.width() >= screenWidth * 45 / 100 &&
                it.bounds.height() >= screenHeight * 55 / 100
        }
    }

    private suspend fun handleList(nodes: List<NodeRef>) {
        val visible = extractVisibleConversations(nodes)
        if (visible.isEmpty()) return
        val target = pickTarget(visible) ?: return
        val now = System.currentTimeMillis()
        val targetNickNorm = norm(target.nickname)
        if (targetNickNorm.isNotBlank() &&
            targetNickNorm == lastListClickNickNorm &&
            now - lastListClickAtMs < LIST_CLICK_COOLDOWN_MS
        ) {
            return
        }
        val sessionKey = buildSessionKey(target.nickname)
        val sig = norm(target.previewText)
        if (sig.isNotBlank()) previewSeenSig[sessionKey] = sig

        expectedNickname = target.nickname
        expectedSetAtMs = System.currentTimeMillis()

        val ok = openConversationFromListByGesture(target)
        if (!ok) {
            expectedNickname = ""
            expectedSetAtMs = 0L
            return
        }
        lastListClickAtMs = now
        lastListClickNickNorm = targetNickNorm
        if (target.waitNeedReply) {
            logi("等待信号触发: ${target.nickname} wait='${target.waitText}' age_sec=${target.waitAgeSec}")
        } else if (target.unreadCount > 0) {
            logi("未读信号触发: ${target.nickname} unread=${target.unreadCount}")
        } else {
            logi("预览触发: ${target.nickname} preview='${target.previewText.take(30)}'")
        }
    }

    private suspend fun ensureCustomerListPage(nodes: List<NodeRef>) {
        val now = System.currentTimeMillis()
        if (now - lastNavFixAtMs < NAV_FIX_COOLDOWN_MS) return

        var acted = false
        var currentNodes = nodes
        var clickedBottomMessage = false

        if (!isBottomMessagePage(currentNodes)) {
            val msgTab = currentNodes.firstOrNull {
                it.viewId == "com.xingin.eva:id/mMessageTabView" && it.enabled && isVisibleNode(it)
            } ?: currentNodes.firstOrNull {
                it.text == "消息" && it.bounds.top >= 1850 && it.enabled && isVisibleNode(it)
            }
            if (msgTab != null && tapOrClick(msgTab)) {
                logi("nav fix: clicked 底部消息")
                acted = true
                clickedBottomMessage = true
                delay(420)
                currentNodes = rootInActiveWindow?.let { flatten(it) } ?: emptyList()
            }
        }

        val shouldTryReception = clickedBottomMessage || !isCustomerReceptionHeader(currentNodes)
        if (shouldTryReception) {
            val recvTab = currentNodes.firstOrNull {
                it.text == "客服接待" && it.enabled && isVisibleNode(it)
            }
            if (recvTab != null && tapOrClick(recvTab)) {
                logi("nav fix: clicked 顶部客服接待")
                acted = true
                delay(420)
                currentNodes = rootInActiveWindow?.let { flatten(it) } ?: emptyList()
            }
        }

        if (!hasSessionTabs(currentNodes)) {
            val currentTab = currentNodes.firstOrNull {
                it.text == "当前会话" && it.enabled && isVisibleNode(it)
            }
            if (currentTab != null && tapOrClick(currentTab)) {
                logi("nav fix: clicked 当前会话")
                acted = true
                delay(420)
                currentNodes = rootInActiveWindow?.let { flatten(it) } ?: emptyList()
            }
        }

        if (acted) {
            lastNavFixAtMs = now
            val reached = isListPage(currentNodes)
            val bottomOk = isBottomMessagePage(currentNodes)
            val topOk = isCustomerReceptionHeader(currentNodes)
            logi("nav fix verify: bottom=$bottomOk top=$topOk list=$reached")
        }
    }

    private fun isBottomMessagePage(nodes: List<NodeRef>): Boolean {
        val hasMessageTab = nodes.any {
            it.viewId == "com.xingin.eva:id/mMessageTabView" && isVisibleNode(it)
        } || nodes.any {
            it.text == "消息" && it.bounds.top >= 1850 && isVisibleNode(it)
        }
        val hasReceptionHeader = nodes.any { it.text == "客服接待" && isVisibleNode(it) } &&
            nodes.any { it.text == "在线" && isVisibleNode(it) } &&
            nodes.any { it.text == "排队数" && isVisibleNode(it) } &&
            nodes.any { it.text.contains("今日接待") && isVisibleNode(it) }
        return hasMessageTab && (hasReceptionHeader || hasSessionTabs(nodes))
    }

    private fun isCustomerReceptionHeader(nodes: List<NodeRef>): Boolean {
        return nodes.any { it.text == "客服接待" && isVisibleNode(it) } &&
            nodes.any { it.text == "在线" && isVisibleNode(it) } &&
            nodes.any { it.text == "排队数" && isVisibleNode(it) } &&
            nodes.any { it.text.contains("今日接待") && isVisibleNode(it) }
    }

    private fun hasSessionTabs(nodes: List<NodeRef>): Boolean {
        return nodes.any { it.text == "当前会话" && isVisibleNode(it) } &&
            nodes.any { it.text == "全部会话" && isVisibleNode(it) } &&
            nodes.any { it.text == "收藏会话" && isVisibleNode(it) }
    }

    private fun isVisibleNode(n: NodeRef): Boolean {
        val b = n.bounds
        return n.enabled && b.width() > 0 && b.height() > 0 && b.bottom > 0
    }

    private fun pickTarget(visible: List<PendingConversation>): PendingConversation? {
        val waitTargets = visible.filter { it.waitNeedReply }
        if (waitTargets.isNotEmpty()) {
            return waitTargets
                .sortedWith(compareByDescending<PendingConversation> { it.waitAgeSec }.thenBy { it.y })
                .firstOrNull()
        }

        val unreadTargets = visible.filter { it.unreadCount > 0 }
        if (unreadTargets.isNotEmpty()) {
            return unreadTargets
                .sortedWith(compareByDescending<PendingConversation> { it.unreadCount }.thenBy { it.y })
                .firstOrNull()
        }

        val previewTargets = visible.filter { it.previewText.isNotBlank() }.filter {
            val sessionKey = buildSessionKey(it.nickname)
            val seen = previewSeenSig[sessionKey].orEmpty()
            seen.isBlank() || seen != norm(it.previewText)
        }
        return previewTargets.firstOrNull()
    }

    private suspend fun handleChat(nodes: List<NodeRef>) {
        lastListClickAtMs = 0L
        lastListClickNickNorm = ""
        val nickname = extractChatNickname(nodes).ifBlank {
            if (expectedNickname.isNotBlank() && System.currentTimeMillis() - expectedSetAtMs <= chatTargetTimeoutMs) {
                expectedNickname
            } else {
                ""
            }
        }
        if (nickname.isBlank()) return
        if (nickname.contains("连接中") || nickname.contains("加载中")) {
            logi("会话加载中，等待稳定标题: $nickname")
            delay(180)
            return
        }

        if (expectedNickname.isNotBlank() && System.currentTimeMillis() - expectedSetAtMs <= chatTargetTimeoutMs) {
            if (norm(nickname) != norm(expectedNickname)) {
                logw("非目标会话，返回列表: $nickname")
                performGlobalAction(GLOBAL_ACTION_BACK)
                delay(300)
                return
            }
        } else if (expectedNickname.isNotBlank()) {
            expectedNickname = ""
            expectedSetAtMs = 0L
        }

        val sessionKey = buildSessionKey(nickname)
        val now = System.currentTimeMillis()
        if (activeChatSessionKey != sessionKey) {
            activeChatSessionKey = sessionKey
            activeChatProgress = null
            chatFrameSeq = 0L
            chatEnteredAtMs = now
            logi("[chat:$nickname] enter_session stabilizing ${CHAT_ENTER_STABILIZE_MS}ms")
            return
        }
        val enterElapsed = now - chatEnteredAtMs
        if (chatEnteredAtMs > 0L && enterElapsed < CHAT_ENTER_STABILIZE_MS) {
            logi("[chat:$nickname] enter_stabilizing remain_ms=${CHAT_ENTER_STABILIZE_MS - enterElapsed}")
            return
        }
        if (now < postSendStableUntilMs) {
            val remain = postSendStableUntilMs - now
            logi("[chat:$nickname] send_stabilizing remain_ms=$remain")
            return
        }
        val rows = extractChatRows(nodes, nickname)
        if (rows.isEmpty()) {
            if (expectedNickname.isNotBlank() && System.currentTimeMillis() - expectedSetAtMs > chatTargetTimeoutMs) {
                logw("目标会话建连超时，清空目标: $expectedNickname")
                expectedNickname = ""
                expectedSetAtMs = 0L
            }
            return
        }
        chatFrameSeq += 1
        logRowsSummary(nickname, rows, marker = "frame#$chatFrameSeq:pre")

        val candidates = buildChatCandidates(rows)
        logCandidateSummary(nickname, candidates, marker = "frame#$chatFrameSeq:pre")
        if (activeChatSessionKey != sessionKey || activeChatProgress == null) {
            activeChatSessionKey = sessionKey
            val progress = ChatSessionProgress(
                idleRounds = 0,
                sessionSeedMs = System.currentTimeMillis(),
            )
            val lastSelfIndex = rows.indexOfLast { it.isSelf }
            if (lastSelfIndex >= 0) {
                val ignore = candidates
                    .filter { it.rowIndex <= lastSelfIndex }
                    .map { it.key }
                progress.initialIgnoreKeys.addAll(ignore)
            }
            activeChatProgress = progress
            logi(
                "[chat:$nickname] init_session last_self_idx=$lastSelfIndex " +
                    "initial_ignore=${progress.initialIgnoreKeys.size}"
            )
        }
        val progress = activeChatProgress ?: return
        val sourceFirst = candidates.filter {
            it.key !in progress.initialIgnoreKeys && it.key !in progress.handledKeys
        }
        logi(
            "[chat:$nickname] frame#$chatFrameSeq keys_state initial_ignore=${progress.initialIgnoreKeys.size} " +
                "handled=${progress.handledKeys.size}"
        )
        logi(
            "[chat:$nickname] pending_source size=${sourceFirst.size}: " +
                sourceFirst.joinToString(" || ") {
                    "idx=${it.rowIndex} key='${it.key}' top=${it.row.top} text='${it.row.text.take(60)}'"
                }
        )
        if (sourceFirst.isEmpty()) {
            progress.idleRounds += 1
            if (progress.idleRounds >= 2) {
                backToListAndClearChatProgress()
            }
            return
        }

        delay(PRE_CONSUME_STABLE_DELAY_MS)
        val root2 = rootInActiveWindow ?: run {
            logw("[chat:$nickname] pending_verify abort: root_null")
            return
        }
        val pkg2 = root2.packageName?.toString().orEmpty()
        if (pkg2 != PKG_QIANFAN) {
            logw("[chat:$nickname] pending_verify abort: pkg='$pkg2'")
            return
        }
        val nodes2 = flatten(root2)
        val nickname2 = extractChatNickname(nodes2).ifBlank { nickname }
        if (norm(nickname2) != norm(nickname)) {
            logw("[chat:$nickname] pending_verify abort: chat_switched_to='$nickname2'")
            return
        }
        val rows2 = extractChatRows(nodes2, nickname)
        if (rows2.isEmpty()) {
            logw("[chat:$nickname] pending_verify abort: rows_empty")
            return
        }
        val candidates2 = buildChatCandidates(rows2)
        val sourceSecond = candidates2.filter {
            it.key !in progress.initialIgnoreKeys && it.key !in progress.handledKeys
        }
        val keys1 = sourceFirst.map { it.key }
        val keys2 = sourceSecond.map { it.key }
        if (keys1 != keys2) {
            logw(
                "[chat:$nickname] pending_verify unstable: first=${keys1.joinToString(",")} " +
                    "second=${keys2.joinToString(",")} skip_round"
            )
            return
        }

        progress.idleRounds = 0
        expectedSetAtMs = System.currentTimeMillis()

        val pendingRows = sourceSecond
        val cfg = WorkerPrefs.load(this)
        val client = DecisionApiClient(cfg)
        for ((idx, cand) in pendingRows.withIndex()) {
            val row = cand.row
            val msgKey = cand.key
            val msg = ExtractedMessage(
                text = row.text,
                sig = row.sig,
                messageType = row.messageType,
                imageBoundsSig = row.imageBoundsSig,
            )
            val eventMessageId = getOrAllocateEventMessageId(
                sessionKey = sessionKey,
                progress = progress,
                msgKey = msgKey,
            )
            val isImageMsg = msg.messageType == "image"
            if (isImageMsg) {
                val notBefore = imageRetryNotBeforeByKey[msgKey] ?: 0L
                if (notBefore > System.currentTimeMillis()) {
                    continue
                }
            }
            val raw = mutableMapOf<String, Any>(
                "source_app" to "xhs_qianfan",
                "send_text" to !isImageMsg,
                "send_image" to isImageMsg,
                "device_serial" to cfg.deviceSerial,
            )
            var previewOpened = false
            if (isImageMsg) {
                previewOpened = openImagePreviewForCapture(msg.imageBoundsSig, nickname)
                if (!previewOpened) {
                    val recovered = closeImagePreviewToChat(nickname)
                    val failCount = (progress.imagePreviewOpenFailCounts[msgKey] ?: 0) + 1
                    progress.imagePreviewOpenFailCounts[msgKey] = failCount
                    if (recovered && failCount >= IMAGE_PREVIEW_OPEN_MAX_FAILS) {
                        progress.handledKeys.add(msgKey)
                        progress.keyMessageIds.remove(msgKey)
                        progress.imagePreviewOpenFailCounts.remove(msgKey)
                        imageRetryNotBeforeByKey.remove(msgKey)
                        logw(
                            "[$sessionKey] image preview open failed $failCount times, " +
                                "mark handled key='$msgKey'"
                        )
                        continue
                    }
                    imageRetryNotBeforeByKey[msgKey] = System.currentTimeMillis() + IMAGE_RETRY_COOLDOWN_MS
                    if (!recovered) {
                        logw(
                            "[$sessionKey] image preview open failed and recover failed, " +
                                "cooldown key='$msgKey' fail_count=$failCount"
                        )
                    } else {
                        logw(
                            "[$sessionKey] image preview open failed, recovered to chat, " +
                                "cooldown key='$msgKey' fail_count=$failCount"
                        )
                    }
                    continue
                }
                progress.imagePreviewOpenFailCounts.remove(msgKey)
                // 让 gateway 只做只读 adb screencap，不再执行 tap/back/uiautomator。
                raw["image_bounds"] = "fullscreen"
                raw["chat_image_bounds"] = msg.imageBoundsSig
                raw["image_capture_mode"] = "worker_preview_gateway_readonly"
            }
            val event = IncomingEvent(
                tenantId = cfg.tenantId,
                platform = "xiaohongshu",
                storeId = cfg.storeId,
                storeName = cfg.storeName,
                customerId = nickname,
                platformNickname = nickname,
                messageId = eventMessageId,
                messageType = msg.messageType,
                text = if (isImageMsg) "" else msg.text,
                mediaUrl = "",
                timestampMs = System.currentTimeMillis(),
                raw = raw,
            )

            val decision = runCatching {
                logi("[$sessionKey] decide start, msg='${event.text.take(40)}'")
                withTimeout(60_000L) { client.decide(event) }
            }.getOrElse { e ->
                val err = e.javaClass.simpleName
                logw("[$sessionKey] decide failed: $err")
                if (isImageMsg && (err == "JobCancellationException" || e is CancellationException)) {
                    imageRetryNotBeforeByKey[msgKey] = System.currentTimeMillis() + IMAGE_RETRY_COOLDOWN_MS
                    logw("[$sessionKey] image decide cancelled, cooldown key='$msgKey'")
                    return@getOrElse null
                }
                DecisionResult(
                    action = if (cfg.fallbackText.isNotBlank()) "send" else "skip",
                    replyText = cfg.fallbackText,
                    reason = "fallback:$err",
                    traceId = "",
                )
            }
            if (previewOpened) {
                val closed = closeImagePreviewToChat(nickname)
                if (!closed) {
                    imageRetryNotBeforeByKey[msgKey] = System.currentTimeMillis() + IMAGE_RETRY_COOLDOWN_MS
                    logw("[$sessionKey] image preview close failed, cooldown key='$msgKey'")
                    continue
                }
            }
            if (decision == null) continue

            if (decision.action != "send" || decision.replyText.isBlank()) {
                runCatching { client.ack(event, decision, sentText = "", status = "skipped") }
                progress.handledKeys.add(msgKey)
                progress.keyMessageIds.remove(msgKey)
                progress.imagePreviewOpenFailCounts.remove(msgKey)
                imageRetryNotBeforeByKey.remove(msgKey)
                logi("[$sessionKey] mark_handled(skip) key='$msgKey' mid='${event.messageId}' handled=${progress.handledKeys.size}")
                continue
            }

            logi("[$sessionKey] send_attempt key='$msgKey' mid='${event.messageId}' reply_len=${decision.replyText.length}")
            val sentOk = sendText(decision.replyText)
            if (sentOk) {
                runCatching {
                    client.ack(event, decision, sentText = decision.replyText, status = "sent")
                }
                progress.handledKeys.add(msgKey)
                progress.keyMessageIds.remove(msgKey)
                progress.imagePreviewOpenFailCounts.remove(msgKey)
                imageRetryNotBeforeByKey.remove(msgKey)
                expectedSetAtMs = System.currentTimeMillis()
                postSendStableUntilMs = System.currentTimeMillis() + POST_SEND_STABILIZE_MS
                logi("[$sessionKey] 已发送回复")
                logi("[$sessionKey] mark_handled(sent) key='$msgKey' mid='${event.messageId}' handled=${progress.handledKeys.size}")
                logChatSnapshotNow(nickname, marker = "post_send")
                if (idx < pendingRows.lastIndex) delay(120)
            } else {
                runCatching {
                    client.ack(event, decision, sentText = decision.replyText, status = "failed")
                }
                if (isImageMsg) {
                    imageRetryNotBeforeByKey[msgKey] = System.currentTimeMillis() + IMAGE_RETRY_COOLDOWN_MS
                }
                logw("[$sessionKey] 发送失败，等待下轮重试")
                logChatSnapshotNow(nickname, marker = "post_send_fail")
                // Keep strict order: stop current batch on first send failure.
                break
            }
        }
    }

    private suspend fun openConversationFromListByGesture(target: PendingConversation): Boolean {
        val b = target.clickNode.bounds
        val y = b.centerY().coerceIn(260, 1860)
        val primaryX = (b.left + (b.width() * 35 / 100)).coerceIn(200, 840)
        val fallbackX = b.centerX().coerceIn(260, 920)
        val points = listOf(
            primaryX to y,
            fallbackX to y,
        )
        for ((idx, p) in points.withIndex()) {
            val (x, ty) = p
            val tapped = dispatchGestureTap(x, ty)
            if (!tapped) {
                logw("列表手势派发失败: ${target.nickname} try=${idx + 1}")
                continue
            }
            if (waitChatPageReady(target.nickname, waitMs = 2200L)) {
                return true
            }
            logw("列表手势点击未进入聊天页，准备重试: ${target.nickname} try=${idx + 1}")
        }
        logw("列表手势点击失败: ${target.nickname}")
        return false
    }

    private suspend fun openImagePreviewForCapture(imageBoundsSig: String, nickname: String): Boolean {
        val bounds = parseBoundsSig(imageBoundsSig) ?: return false
        val x = bounds.centerX().coerceIn(120, 960)
        val y = bounds.centerY().coerceIn(220, 1940)
        if (!dispatchGestureTap(x, y)) return false
        delay(IMAGE_PREVIEW_ENTER_WAIT_MS)
        val nodes = snapshotActiveNodes()
        if (nodes.isEmpty()) {
            logw("[chat:$nickname] image preview state unknown: tree_empty")
            return false
        }
        val entered = !isChatPage(nodes)
        if (!entered) {
            logw("[chat:$nickname] image preview not entered")
        }
        return entered
    }

    private suspend fun closeImagePreviewToChat(nickname: String): Boolean {
        repeat(3) { attempt ->
            val nodes = snapshotActiveNodes()
            if (nodes.isNotEmpty() && isChatPage(nodes)) return true
            if (nodes.isEmpty()) {
                delay(160)
                return@repeat
            }
            // 千帆图片预览页优先点图退出，BACK 作为兜底。
            if (attempt == 0) {
                dispatchGestureTap(540, 1060)
                delay(380)
            } else {
                performGlobalAction(GLOBAL_ACTION_BACK)
                delay(260)
            }
        }
        val ok = snapshotActiveNodes().let { it.isNotEmpty() && isChatPage(it) }
        if (!ok) {
            logw("[chat:$nickname] preview exit failed: still_not_chat")
        }
        return ok
    }

    private suspend fun snapshotActiveNodes(rounds: Int = 4, delayMs: Long = 120L): List<NodeRef> {
        repeat(rounds) { idx ->
            val root = rootInActiveWindow
            if (root != null) return flatten(root)
            if (idx < rounds - 1) delay(delayMs)
        }
        return emptyList()
    }

    private suspend fun waitChatPageReady(expectedNick: String, waitMs: Long): Boolean {
        val rounds = max(1, (waitMs / 120L).toInt())
        repeat(rounds) {
            delay(120)
            val root = rootInActiveWindow ?: return@repeat
            val nodes = flatten(root)
            if (!isChatPage(nodes)) return@repeat
            if (expectedNick.isBlank()) return true
            val chatNick = extractChatNickname(nodes)
            if (chatNick.isBlank() || norm(chatNick) == norm(expectedNick)) {
                return true
            }
        }
        return false
    }

    private fun extractVisibleConversations(nodes: List<NodeRef>): List<PendingConversation> {
        val rawRows = nodes.filter {
            it.clickable &&
                it.bounds.width() >= 850 &&
                it.bounds.height() in 110..280 &&
                it.bounds.top in 420..1550 &&
                it.bounds.left <= 40
        }.sortedBy { it.bounds.centerY() }

        if (rawRows.isEmpty()) return emptyList()

        val rows = mutableListOf<NodeRef>()
        for (row in rawRows) {
            if (rows.isEmpty()) {
                rows += row
                continue
            }
            val dist = abs(rows.last().bounds.centerY() - row.bounds.centerY())
            if (dist > 40) rows += row
        }

        val out = mutableListOf<PendingConversation>()
        for (row in rows) {
            val rowNodes = flattenSubtree(row.raw)
                .filter { it.raw != row.raw }
            val hasLeftAvatar = rowNodes.any {
                it.clazz.endsWith("ImageView") &&
                    it.bounds.left <= 180 &&
                    it.bounds.width() in 60..180 &&
                    it.bounds.height() in 60..180
            }
            if (!hasLeftAvatar) continue

            val inRow = rowNodes.filter { it.text.isNotBlank() }
            if (inRow.isEmpty()) continue

            val rowMidY = row.bounds.top + row.bounds.height() / 2
            val nicknameNode = inRow
                .filter {
                    !isSystemText(it.text) &&
                        !isTimeLike(it.text) &&
                        !looksLikeWaitText(it.text) &&
                        // 昵称几何区域：头像右侧、行上半区、时间列左侧
                        it.bounds.centerX() in 170..760 &&
                        it.bounds.centerY() <= rowMidY + 20
                }
                .minWithOrNull(
                    compareBy<NodeRef> {
                        kotlin.math.abs(it.bounds.centerY() - (row.bounds.top + 42))
                    }.thenBy {
                        kotlin.math.abs(it.bounds.centerX() - 250)
                    }
                )
                ?: continue
            val nickname = nicknameNode.text.trim()

            val preview = inRow
                .filter {
                    it.bounds.top >= nicknameNode.bounds.bottom - 20 &&
                        !isTimeLike(it.text) &&
                        !looksLikeWaitText(it.text) &&
                        !isSystemText(it.text) &&
                        it.text.trim() != nickname
                }
                .sortedBy { it.bounds.top }
                .joinToString(" ") { it.text.trim() }
                .trim()

            val waitText = inRow
                .map { it.text.trim() }
                .firstOrNull { looksLikeWaitText(it) }
                .orEmpty()
            val (waitNeedReply, waitAgeSec) = parseNeedReplyWait(waitText)
            val unreadCount = inRow
                .filter { it.bounds.centerX() <= 220 }
                .maxOfOrNull { parseUnreadCount(it.text) }
                ?: 0
            val hasTimeSignal = inRow.any { isTimeLike(it.text) || looksLikeWaitText(it.text) }
            if (!hasTimeSignal && unreadCount <= 0) continue

            out += PendingConversation(
                nickname = nickname,
                previewText = preview,
                waitText = waitText,
                waitNeedReply = waitNeedReply,
                waitAgeSec = waitAgeSec,
                unreadCount = unreadCount,
                y = row.bounds.centerY(),
                clickNode = row,
            )
        }
        return out
    }

    private fun extractChatNickname(nodes: List<NodeRef>): String {
        val scrollRaw = findChatScrollNode(nodes)?.raw
        val scrollIdx = if (scrollRaw != null) nodes.indexOfFirst { it.raw == scrollRaw } else -1
        return nodes
            .withIndex()
            .filter { (idx, it) ->
                (scrollIdx < 0 || idx < scrollIdx) &&
                it.text.isNotBlank() &&
                    it.clazz.endsWith("TextView") &&
                    !isSystemText(it.text) &&
                    it.text != "老客" &&
                    it.text != "新客" &&
                    !it.text.contains("输入中") &&
                    !isTimeLike(it.text)
            }
            .firstOrNull()
            ?.value
            ?.text
            ?.trim()
            .orEmpty()
    }

    private fun extractChatRows(nodes: List<NodeRef>, nickname: String): List<ChatRow> {
        val chatScroll = findChatScrollNode(nodes) ?: run {
            logw("[chat:$nickname] chat_scroll_not_found")
            return emptyList()
        }
        val rows = extractChatRowBands(chatScroll.raw)
        if (rows.isEmpty()) return emptyList()
        logi("[chat:$nickname] wrappers_total=${rows.size}")

        val out = mutableListOf<ChatRow>()
        for ((idx, rowBand) in rows.withIndex()) {
            val structure = analyzeRowStructure(rowBand.raw)
            val contentBranch = structure?.contentBranch ?: continue
            val side = structure.side
            if (side == "unknown") continue

            val contentNodes = flattenSubtree(contentBranch.raw)
                .filter { it.raw != contentBranch.raw }
            if (contentNodes.isEmpty()) continue

            val textNodes = contentNodes
                .filter { it.clazz.endsWith("TextView") && it.text.isNotBlank() }
            val textValues = textNodes
                .map { it.text.trim() }
                .filter { it.isNotBlank() }

            val contentImages = contentNodes.filter { it.clazz.endsWith("ImageView") }
            if (textValues.isEmpty() && contentImages.isEmpty()) continue

            val text = textValues
                .joinToString(" | ")
                .trim()

            val isImageRow = text.isBlank() && contentImages.isNotEmpty()
            val rowText = if (text.isNotBlank()) text else "[图片]"
            val imageBoundsSig = if (isImageRow) {
                val merged = mergeRects(contentImages.map { it.bounds })
                stableRowBoundsSig(merged)
            } else {
                ""
            }
            val rowMessageType = if (isImageRow) "image" else "text"
            val textSegs = if (rowText.isBlank()) 0 else rowText.split(" | ").size
            logi(
                "[chat:$nickname] bubble idx=$idx side=$side left_avatar=${structure.avatarBranch != null && side == "left"} " +
                    "right_avatar=${structure.avatarBranch != null && side == "right"} " +
                    "top=${rowBand.bounds.top} h=${rowBand.bounds.height()} text_nodes=${textNodes.size} " +
                    "images=${contentImages.size} segs=$textSegs text_len=${rowText.length} text='${rowText}'"
            )

            val isSelf = side == "right"
            val sig = stableMessageSig(
                nickname = nickname,
                isSelf = isSelf,
                text = rowText,
                messageType = rowMessageType,
                imageBoundsSig = imageBoundsSig,
            )
            out += ChatRow(
                top = idx,
                text = rowText,
                isSelf = isSelf,
                sig = sig,
                messageType = rowMessageType,
                imageBoundsSig = imageBoundsSig,
            )
        }
        return out
    }

    private fun findChatScrollNode(nodes: List<NodeRef>): NodeRef? {
        return nodes
            .filter {
                it.clazz.endsWith("ScrollView") && it.raw != null
            }
            .maxByOrNull { flattenSubtree(it.raw).size }
    }

    private fun extractChatRowBands(chatScrollRaw: AccessibilityNodeInfo?): List<NodeRef> {
        if (chatScrollRaw == null) return emptyList()
        val candidates = mutableListOf<AvatarRowCandidate>()

        fun walk(node: AccessibilityNodeInfo?, depth: Int) {
            if (node == null) return
            if (depth > 10) return

            val structure = analyzeRowStructure(node)
            if (structure != null && structure.side != "unknown" && structure.avatarBranch != null && structure.contentBranch != null) {
                candidates += AvatarRowCandidate(
                    rowRef = nodeToRef(node),
                    structure = structure,
                    depth = depth,
                )
            }

            for (i in 0 until node.childCount) {
                walk(node.getChild(i), depth + 1)
            }
        }

        walk(chatScrollRaw, 0)
        if (candidates.isEmpty()) return emptyList()

        // One avatar = one row; if multiple candidates share avatar, keep the deepest (most specific) one.
        val picked = candidates
            .groupBy { c ->
                val a = c.structure.avatarBranch!!.bounds
                "${a.left},${a.top},${a.right},${a.bottom}"
            }
            .values
            .mapNotNull { list ->
                list.maxWithOrNull(
                    compareBy<AvatarRowCandidate> { it.depth }
                        .thenBy { it.rowRef.bounds.width() * it.rowRef.bounds.height() }
                )
            }
            .map { it.rowRef }
            .distinctBy { r ->
                val a = analyzeRowStructure(r.raw)?.avatarBranch?.bounds
                if (a != null) "${a.left},${a.top},${a.right},${a.bottom}" else "${r.bounds.left},${r.bounds.top},${r.bounds.right},${r.bounds.bottom}"
            }
            .sortedBy { r ->
                analyzeRowStructure(r.raw)?.avatarBranch?.bounds?.centerY() ?: r.bounds.centerY()
            }

        return picked
    }

    private data class RowBranchSummary(
        val index: Int,
        val branch: NodeRef,
        val hasAnyText: Boolean,
        val hasImage: Boolean,
        val hasFocusableImage: Boolean,
    )

    private fun analyzeRowStructure(rowRaw: AccessibilityNodeInfo?): RowStructure? {
        val children = directChildRefs(rowRaw)
        if (children.isEmpty()) return null

        val branches = children.mapIndexed { idx, child ->
            val sub = flattenSubtree(child.raw)
            val texts = sub
                .map { it.text.trim() }
                .filter { it.isNotBlank() }
            val hasAnyText = texts.isNotEmpty()
            val images = sub.filter { it.clazz.endsWith("ImageView") }
            val hasImage = images.isNotEmpty()
            val hasFocusableImage = images.any { it.raw?.isFocusable == true }
            RowBranchSummary(
                index = idx,
                branch = child,
                hasAnyText = hasAnyText,
                hasImage = hasImage,
                hasFocusableImage = hasFocusableImage,
            )
        }

        val avatar = branches.firstOrNull {
            it.hasImage && !it.hasFocusableImage && !it.hasAnyText
        } ?: return null

        val content = branches
            .asSequence()
            .filter { it.index != avatar.index }
            .filter { it.hasAnyText || it.hasFocusableImage }
            .minWithOrNull(
                compareBy<RowBranchSummary> {
                    abs(it.branch.bounds.top - avatar.branch.bounds.top)
                }.thenBy {
                    abs(it.branch.bounds.left - avatar.branch.bounds.left)
                }.thenBy { it.index }
            )
            ?: return null

        val avatarCx = avatar.branch.bounds.centerX()
        val contentCx = content.branch.bounds.centerX()
        val side = when {
            contentCx > avatarCx -> "left"
            contentCx < avatarCx -> "right"
            else -> "unknown"
        }

        return RowStructure(
            avatarBranch = avatar.branch,
            contentBranch = content.branch,
            side = side,
        )
    }

    private fun directChildRefs(node: AccessibilityNodeInfo?): List<NodeRef> {
        if (node == null) return emptyList()
        val out = mutableListOf<NodeRef>()
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            out += nodeToRef(child)
        }
        return out
    }

    private fun findChatInputNode(nodes: List<NodeRef>): NodeRef? {
        return nodes.firstOrNull { it.clazz.endsWith("EditText") && it.enabled }
    }

    private fun isChatMetaText(text0: String): Boolean {
        val text = text0.trim()
        if (text.isBlank()) return true
        if (text == "已读" || text == "未读") return true
        return isTimeLike(text)
    }

    private suspend fun backToListAndClearChatProgress() {
        var reachedList = false
        repeat(2) {
            performGlobalAction(GLOBAL_ACTION_BACK)
            delay(220)
            val root = rootInActiveWindow ?: return@repeat
            val nodes = flatten(root)
            if (isListPage(nodes)) {
                reachedList = true
                return@repeat
            }
        }
        if (!reachedList) {
            logw("返回列表未确认成功，仍强制清理会话状态")
        }
        activeChatSessionKey = ""
        activeChatProgress = null
        chatEnteredAtMs = 0L
        expectedNickname = ""
        expectedSetAtMs = 0L
    }

    private suspend fun sendText(text: String): Boolean {
        val nodes = rootInActiveWindow?.let { flatten(it) } ?: return false
        val inputNode = findChatInputNode(nodes) ?: run {
            logw("sendText fail: input_missing")
            return false
        }

        var setOk = setNodeText(inputNode.raw, text)
        if (!setOk) {
            clickNode(inputNode)
            delay(80)
            setOk = setNodeText(inputNode.raw, text)
        }
        if (!setOk) {
            logw("sendText fail: set_text_fail")
            return false
        }
        delay(140)

        repeat(2) { attempt ->
            val nodes2 = rootInActiveWindow?.let { flatten(it) } ?: run {
                logw("sendText fail: tree_empty_before_click")
                return false
            }
            val sendTextNode = nodes2.firstOrNull {
                it.text == "发送" && it.clickable && it.enabled
            } ?: nodes2.firstOrNull { it.text == "发送" && it.enabled }

            if (sendTextNode == null) {
                logw("sendText fail: send_button_missing attempt=${attempt + 1}")
                delay(120)
                return@repeat
            }

            val center = sendTextNode.bounds
            val tapOk = dispatchGestureTap(center.centerX(), center.centerY())
            val clickOk = if (!tapOk) clickNode(sendTextNode) else true
            if (!clickOk) {
                logw("sendText fail: click_send_fail attempt=${attempt + 1}")
                delay(120)
                return@repeat
            }
            delay(180)
            confirmSendReminderIfAny()

            val waitMs = if (attempt == 0) 2200L else 1400L
            if (waitInputChangedAfterSend(text, waitMs = waitMs)) {
                return true
            }
            delay(120)
        }

        val nodes2 = rootInActiveWindow?.let { flatten(it) } ?: emptyList()
        val curr = findChatInputNode(nodes2)?.text?.trim().orEmpty()
        logw("sendText fail: input_stuck curr='${curr.take(32)}'")
        return false
    }

    private suspend fun confirmSendReminderIfAny(): Boolean {
        val nodes = rootInActiveWindow?.let { flatten(it) } ?: return false
        val hasTitle = nodes.any { it.text == "服务提醒：建议修改话术" && isVisibleNode(it) }
        if (!hasTitle) return false
        val confirm = nodes.firstOrNull {
            it.text == "确认发送" && it.enabled && isVisibleNode(it)
        } ?: return false
        val ok = clickNode(confirm) || dispatchGestureTap(confirm.bounds.centerX(), confirm.bounds.centerY())
        if (ok) {
            logw("检测到建议修改话术弹窗，已点确认发送")
            delay(220)
        }
        return ok
    }

    private suspend fun waitInputChangedAfterSend(sentText: String, waitMs: Long): Boolean {
        val rounds = max(1, (waitMs / 150L).toInt())
        repeat(rounds) {
            delay(150)
            val nodes = rootInActiveWindow?.let { flatten(it) } ?: return@repeat
            val curr = findChatInputNode(nodes)?.text?.trim().orEmpty()
            if (curr.isBlank() || norm(curr) != norm(sentText)) {
                return true
            }
        }
        return false
    }

    private fun dismissInstallPopupIfAny(nodes: List<NodeRef>): Boolean {
        val hasInstallTitle = nodes.any { it.text == "安装" || it.text.contains("新版本") }
        if (!hasInstallTitle) return false
        val cancel = nodes.firstOrNull { it.text == "取消" }
        if (cancel != null) {
            val ok = clickNode(cancel)
            if (ok) {
                logw("检测到安装弹窗，已点取消")
                return true
            }
        }
        return false
    }

    private fun dismissServiceSummaryPopupIfAny(nodes: List<NodeRef>): Boolean {
        val hasTitle = nodes.any { it.text == "本周店铺客服服务数据总结" }
        if (!hasTitle) return false
        val close = nodes.firstOrNull { it.text == "关闭" && it.clickable }
            ?: nodes.firstOrNull { it.text == "关闭" }
        if (close != null) {
            val ok = clickNode(close)
            if (ok) {
                logw("检测到服务数据总结弹窗，已点关闭")
                return true
            }
        }
        return false
    }

    private fun isCustomerTextCandidate(text: String, nickname: String): Boolean {
        if (text.isBlank()) return false
        if (text == nickname) return false
        if (isSystemText(text)) return false
        if (isTimeLike(text)) return false
        if (text == "老客" || text == "新客") return false
        if (text.matches(Regex("^\\d{1,3}$"))) return false
        if (text.startsWith("订单编号")) return false
        return true
    }

    private fun isSystemText(text0: String): Boolean {
        val text = text0.trim()
        if (text.isBlank()) return true
        if (text == "已读" || text == "未读") return true
        if (text == "客服接待" || text == "当前会话" || text == "全部会话" || text == "收藏会话") return true
        if (text == "没有更多消息了") return true
        if (text.contains("接入会话")) return true
        if (text.contains("会话长时间无新消息")) return true
        if (text.contains("连接中")) return true
        if (text.contains("加载中")) return true
        if (text == "在线" || text == "排队数" || text == "今日接待") return true
        if (text == "收到消息却未听到提示音？" || text == "去看看" || text == "发送") return true
        return false
    }

    private fun isNicknameCandidate(text0: String): Boolean {
        val text = text0.trim()
        if (text.isBlank()) return false
        if (isSystemText(text)) return false
        if (isTimeLike(text)) return false
        if (looksLikeWaitText(text)) return false
        if (text == "99+" || text.matches(Regex("^\\d+$"))) return false
        if (text.length > 24) return false
        if (text.any { it.isWhitespace() }) return false
        return true
    }

    private fun looksLikeWaitText(text0: String): Boolean {
        val text = normalizeCompact(text0)
        return text.matches(Regex("^已等待\\d+秒$")) ||
            text.matches(Regex("^已等待\\d+分$")) ||
            text.matches(Regex("^已等待\\d+分钟$")) ||
            text.matches(Regex("^超过\\d+分$")) ||
            text.matches(Regex("^超过\\d+分钟$")) ||
            text.matches(Regex("^超过\\d+小时$")) ||
            text.matches(Regex("^\\d+秒$")) ||
            text.matches(Regex("^\\d+分$")) ||
            text.matches(Regex("^\\d+分钟$")) ||
            text.matches(Regex("^\\d+分钟前$"))
    }

    private fun parseNeedReplyWait(text0: String): Pair<Boolean, Int> {
        val text = normalizeCompact(text0)
        if (text.isBlank()) return false to 0
        Regex("^超过(\\d+)小时$").matchEntire(text)?.let { return true to max(1, it.groupValues[1].toInt() * 3600) }
        Regex("^超过(\\d+)分$").matchEntire(text)?.let { return true to max(1, it.groupValues[1].toInt() * 60) }
        Regex("^超过(\\d+)分钟$").matchEntire(text)?.let { return true to max(1, it.groupValues[1].toInt() * 60) }
        Regex("^已等待(\\d+)秒$").matchEntire(text)?.let { return true to max(1, it.groupValues[1].toInt()) }
        Regex("^已等待(\\d+)分$").matchEntire(text)?.let { return true to max(1, it.groupValues[1].toInt() * 60) }
        Regex("^已等待(\\d+)分钟$").matchEntire(text)?.let { return true to max(1, it.groupValues[1].toInt() * 60) }
        Regex("^(\\d+)秒$").matchEntire(text)?.let { return true to max(1, it.groupValues[1].toInt()) }
        Regex("^(\\d+)分$").matchEntire(text)?.let { return true to max(1, it.groupValues[1].toInt() * 60) }
        Regex("^(\\d+)分钟$").matchEntire(text)?.let { return true to max(1, it.groupValues[1].toInt() * 60) }
        Regex("^(\\d+)分钟前$").matchEntire(text)?.let { return true to max(1, it.groupValues[1].toInt() * 60) }
        return false to 0
    }

    private fun parseUnreadCount(text0: String): Int {
        val text = normalizeCompact(text0)
        if (text == "99+") return 99
        if (text.matches(Regex("^\\d+$"))) return text.toIntOrNull() ?: 0
        return 0
    }

    private fun isTimeLike(text0: String): Boolean {
        val text = text0.trim()
        if (text.isBlank()) return false
        return text.matches(Regex("^\\d{1,2}:\\d{2}$")) ||
            text.matches(Regex("^(今天|昨天)\\s*\\d{1,2}:\\d{2}$")) ||
            text.matches(Regex("^\\d{2}-\\d{2}\\s+\\d{1,2}:\\d{2}$")) ||
            text.matches(Regex("^\\d{4}-\\d{2}-\\d{2}\\s+\\d{1,2}:\\d{2}:\\d{2}$"))
    }

    private fun setNodeText(node: AccessibilityNodeInfo?, text: String): Boolean {
        if (node == null) return false
        val b = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        }
        return node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, b)
    }

    private fun clickNode(node: NodeRef): Boolean {
        var curr: AccessibilityNodeInfo? = node.raw
        repeat(6) {
            if (curr == null) return@repeat
            if (curr!!.isClickable && curr!!.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                return true
            }
            curr = curr!!.parent
        }
        return false
    }

    private fun tapOrClick(node: NodeRef): Boolean {
        val b = node.bounds
        val tapOk = dispatchGestureTap(b.centerX(), b.centerY())
        if (tapOk) return true
        return clickNode(node)
    }

    private fun dispatchGestureTap(x: Int, y: Int): Boolean {
        return runCatching {
            val path = android.graphics.Path().apply { moveTo(x.toFloat(), y.toFloat()) }
            val stroke = android.accessibilityservice.GestureDescription.StrokeDescription(path, 0, 80)
            val gesture = android.accessibilityservice.GestureDescription.Builder().addStroke(stroke).build()
            dispatchGesture(gesture, null, null)
        }.getOrElse { false }
    }

    private fun flatten(root: AccessibilityNodeInfo): List<NodeRef> {
        val out = ArrayList<NodeRef>(256)

        fun walk(node: AccessibilityNodeInfo?) {
            if (node == null) return
            out += nodeToRef(node)
            for (i in 0 until node.childCount) {
                walk(node.getChild(i))
            }
        }

        walk(root)
        return out
    }

    private fun flattenSubtree(root: AccessibilityNodeInfo?): List<NodeRef> {
        if (root == null) return emptyList()
        val out = ArrayList<NodeRef>(128)
        fun walk(node: AccessibilityNodeInfo?) {
            if (node == null) return
            out += nodeToRef(node)
            for (i in 0 until node.childCount) {
                walk(node.getChild(i))
            }
        }
        walk(root)
        return out
    }

    private fun nodeToRef(node: AccessibilityNodeInfo): NodeRef {
        val r = Rect()
        node.getBoundsInScreen(r)
        return NodeRef(
            text = node.text?.toString().orEmpty(),
            viewId = node.viewIdResourceName.orEmpty(),
            clazz = node.className?.toString().orEmpty(),
            bounds = r,
            clickable = node.isClickable,
            enabled = node.isEnabled,
            pkg = node.packageName?.toString().orEmpty(),
            raw = node,
        )
    }

    private fun buildSessionKey(nickname: String): String {
        val cfg = WorkerPrefs.load(this)
        return "xiaohongshu:${cfg.storeId}:$nickname"
    }

    private fun getOrAllocateEventMessageId(
        sessionKey: String,
        progress: ChatSessionProgress,
        msgKey: String,
    ): String {
        val cached = progress.keyMessageIds[msgKey]
        if (cached != null) return cached
        val seq = progress.nextMessageSeq
        progress.nextMessageSeq = seq + 1
        val mid = md5Hex("$sessionKey|${progress.sessionSeedMs}|$seq").take(24)
        progress.keyMessageIds[msgKey] = mid
        return mid
    }

    private fun stableRowBoundsSig(r: Rect): String {
        val y1 = (r.top / 10) * 10
        val y2 = (r.bottom / 10) * 10
        val x1 = (r.left / 10) * 10
        val x2 = (r.right / 10) * 10
        return "$x1,$y1,$x2,$y2"
    }

    private fun parseBoundsSig(sig: String): Rect? {
        if (sig.isBlank()) return null
        val p = sig.split(",")
        if (p.size != 4) return null
        val l = p[0].trim().toIntOrNull() ?: return null
        val t = p[1].trim().toIntOrNull() ?: return null
        val r = p[2].trim().toIntOrNull() ?: return null
        val b = p[3].trim().toIntOrNull() ?: return null
        if (r <= l || b <= t) return null
        return Rect(l, t, r, b)
    }

    private fun stableMessageSig(
        nickname: String,
        isSelf: Boolean,
        text: String,
        messageType: String,
        imageBoundsSig: String,
    ): String {
        val side = if (isSelf) "R" else "L"
        val payload = if (messageType == "image") "image" else norm(text)
        val base = "${norm(nickname)}|$side|$messageType|$payload"
        return "$side:${md5Hex(base).take(20)}"
    }

    private fun buildChatCandidates(rows: List<ChatRow>): List<ChatCandidate> {
        val tmp = mutableListOf<ChatCandidate>()
        for ((idx, row) in rows.withIndex()) {
            if (row.isSelf) continue
            tmp += ChatCandidate(
                rowIndex = idx,
                row = row,
                key = "",
            )
        }
        if (tmp.isEmpty()) return emptyList()

        val textCounts = mutableMapOf<String, Int>()
        var imageOrdinal = 0
        val out = tmp.toMutableList()
        for (i in out.indices) {
            val cand = out[i]
            if (cand.row.messageType == "image") {
                // Image key only uses in-chat order; no geometry payload.
                out[i] = cand.copy(key = "L|I|$imageOrdinal")
                imageOrdinal += 1
            } else {
                val base = "L|T|${md5Hex(norm(cand.row.text)).take(10)}"
                val seq = textCounts[base] ?: 0
                textCounts[base] = seq + 1
                out[i] = cand.copy(key = "$base#$seq")
            }
        }
        return out
    }

    private fun mergeRects(rects: List<Rect>): Rect {
        val first = rects.firstOrNull() ?: return Rect(0, 0, 0, 0)
        var left = first.left
        var top = first.top
        var right = first.right
        var bottom = first.bottom
        for (r in rects.drop(1)) {
            if (r.left < left) left = r.left
            if (r.top < top) top = r.top
            if (r.right > right) right = r.right
            if (r.bottom > bottom) bottom = r.bottom
        }
        return Rect(left, top, right, bottom)
    }

    private fun logRowsSummary(nickname: String, rows: List<ChatRow>, marker: String) {
        val digest = rows
            .takeLast(12)
            .joinToString(" || ") {
                val side = if (it.isSelf) "R" else "L"
                "$side@${it.top}:${it.text.take(32)}"
            }
        logi("[chat:$nickname] $marker rows=${rows.size} digest=$digest")
    }

    private fun logCandidateSummary(nickname: String, cands: List<ChatCandidate>, marker: String) {
        if (cands.isEmpty()) {
            logi("[chat:$nickname] $marker candidates=0")
            return
        }
        val baseStats = cands
            .groupBy { it.key.substringBefore("#") }
            .entries
            .sortedByDescending { it.value.size }
            .take(8)
            .joinToString(" | ") { (base, list) ->
                val seqs = list.map { it.key.substringAfter("#", "?") }.sorted().joinToString(",")
                "$base n=${list.size} seq=[$seqs]"
            }
        logi("[chat:$nickname] $marker candidates=${cands.size} bases=$baseStats")
    }

    private fun logChatSnapshotNow(nickname: String, marker: String) {
        val root = rootInActiveWindow ?: run {
            logw("[chat:$nickname] $marker snapshot_root_null")
            return
        }
        val pkg = root.packageName?.toString().orEmpty()
        if (pkg != PKG_QIANFAN) {
            logw("[chat:$nickname] $marker snapshot_pkg_mismatch pkg='$pkg'")
            return
        }
        val nodes = flatten(root)
        val rows = extractChatRows(nodes, nickname)
        logRowsSummary(nickname, rows, marker = marker)
        logCandidateSummary(nickname, buildChatCandidates(rows), marker = marker)
    }

    private fun normalizeCompact(text0: String): String {
        return text0.trim()
            .replace('\u00A0', ' ')
            .replace(Regex("\\s+"), "")
    }

    private fun norm(v: String): String = v.replace("\\s+".toRegex(), "")

    private fun md5Hex(v: String): String {
        val md = MessageDigest.getInstance("MD5")
        val bytes = md.digest(v.toByteArray(Charsets.UTF_8))
        return buildString(bytes.size * 2) {
            for (b in bytes) append("%02x".format(b))
        }
    }

    private fun logi(msg: String) = Log.i(TAG, msg)
    private fun logw(msg: String) = Log.w(TAG, msg)
}
