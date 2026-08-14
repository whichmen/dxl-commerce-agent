package com.dxl.kefu.qianniu

import org.json.JSONArray
import org.json.JSONObject

data class IncomingEvent(
    val tenantId: String,
    val platform: String,
    val storeId: String,
    val storeName: String,
    val customerId: String,
    val platformNickname: String,
    val messageId: String,
    val messageType: String,
    val text: String,
    val mediaUrl: String,
    val timestampMs: Long,
    val raw: Map<String, Any?>,
    val role: String = "customer",
) {
    val sessionKey: String
        get() = "$platform:$storeId:$platformNickname"

    fun toJson(): JSONObject {
        val obj = JSONObject()
        obj.put("tenant_id", tenantId)
        obj.put("platform", platform)
        obj.put("store_id", storeId)
        obj.put("store_name", storeName)
        obj.put("customer_id", customerId)
        obj.put("platform_nickname", platformNickname)
        obj.put("message_id", messageId)
        obj.put("message_type", messageType)
        obj.put("text", text)
        obj.put("media_url", mediaUrl)
        obj.put("timestamp_ms", timestampMs)
        obj.put("raw", mapToJson(raw))
        obj.put("role", role)
        obj.put("session_key", sessionKey)
        return obj
    }
}

data class DecisionResult(
    val action: String,
    val replyText: String,
    val reason: String,
    val traceId: String,
)

data class WorkerOcrItem(
    val itemId: String,
    val bounds: String,
)

data class WorkerOcrResult(
    val itemId: String,
    val ocrText: String,
    val sha1: String,
)

private fun mapToJson(map: Map<String, Any?>): JSONObject {
    val obj = JSONObject()
    map.forEach { (k, v) ->
        when (v) {
            null -> obj.put(k, JSONObject.NULL)
            is Number, is Boolean, is String -> obj.put(k, v)
            is Map<*, *> -> {
                @Suppress("UNCHECKED_CAST")
                obj.put(k, mapToJson(v as Map<String, Any?>))
            }
            is List<*> -> {
                val arr = JSONArray()
                v.forEach { item -> arr.put(item) }
                obj.put(k, arr)
            }
            else -> obj.put(k, v.toString())
        }
    }
    return obj
}
