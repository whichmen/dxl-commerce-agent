package com.dxl.kefu.qianfan

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

class DecisionApiClient(private val cfg: WorkerConfig) {
    private val jsonMedia = "application/json; charset=utf-8".toMediaType()
    private val client = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(75, TimeUnit.SECONDS)
        .writeTimeout(15, TimeUnit.SECONDS)
        .build()

    suspend fun decide(event: IncomingEvent): DecisionResult = withContext(Dispatchers.IO) {
        val payload = JSONObject().put("event", event.toJson())
        val data = postJson("/v1/decide", payload)
        val action = data.optString("action", "skip")
        val replyText = data.optString("reply_text", "")
        val reason = data.optString("reason", "")
        val traceId = data.optJSONObject("extra")?.optString("trace_id", "") ?: ""
        DecisionResult(action = action, replyText = replyText, reason = reason, traceId = traceId)
    }

    suspend fun ack(event: IncomingEvent, decision: DecisionResult, sentText: String, status: String) {
        withContext(Dispatchers.IO) {
            val payload = JSONObject()
                .put("event", event.toJson())
                .put("trace_id", decision.traceId)
                .put("status", status)
                .put("reason", decision.reason)
                .put("sent_text", sentText)
                .put("sent_image_urls", org.json.JSONArray())
            runCatching { postJson("/v1/worker/ack", payload) }
        }
    }

    private fun postJson(path: String, payload: JSONObject): JSONObject {
        val base = cfg.decisionUrl.trimEnd('/')
        val reqBuilder = Request.Builder()
            .url(base + path)
            .post(payload.toString().toRequestBody(jsonMedia))
            .header("Content-Type", "application/json; charset=utf-8")
            .header("Accept", "application/json")
        if (cfg.decisionKey.isNotBlank()) {
            reqBuilder.header("X-Api-Key", cfg.decisionKey)
        }
        client.newCall(reqBuilder.build()).execute().use { resp ->
            if (!resp.isSuccessful) {
                throw IllegalStateException("http_${resp.code}")
            }
            val body = resp.body?.string().orEmpty().trim()
            if (body.isBlank()) return JSONObject()
            return JSONObject(body)
        }
    }
}
