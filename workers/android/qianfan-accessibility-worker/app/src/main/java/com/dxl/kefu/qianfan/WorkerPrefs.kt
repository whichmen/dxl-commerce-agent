package com.dxl.kefu.qianfan

import android.content.Context
import android.provider.Settings

private const val PREFS_NAME = "qianfan_worker_prefs"
private const val KEY_DECISION_URL = "decision_url"
private const val KEY_DECISION_KEY = "decision_key"
private const val KEY_TENANT_ID = "tenant_id"
private const val KEY_STORE_ID = "store_id"
private const val KEY_STORE_NAME = "store_name"
private const val KEY_FALLBACK_TEXT = "fallback_text"
private const val KEY_DEVICE_SERIAL = "device_serial"

data class WorkerConfig(
    val decisionUrl: String,
    val decisionKey: String,
    val tenantId: String,
    val storeId: String,
    val storeName: String,
    val fallbackText: String,
    val deviceSerial: String,
) {
    companion object {
        val DEFAULT = WorkerConfig(
            decisionUrl = "http://127.0.0.1:18080",
            decisionKey = "",
            tenantId = "default",
            storeId = "xiaohongshu_store_example",
            storeName = "示例小红书店铺",
            fallbackText = "稍等",
            deviceSerial = "",
        )
    }
}

object WorkerPrefs {
    fun load(context: Context): WorkerConfig {
        val sp = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val d = WorkerConfig.DEFAULT
        return WorkerConfig(
            decisionUrl = sp.getString(KEY_DECISION_URL, d.decisionUrl).orEmpty().trim(),
            decisionKey = sp.getString(KEY_DECISION_KEY, d.decisionKey).orEmpty().trim(),
            tenantId = sp.getString(KEY_TENANT_ID, d.tenantId).orEmpty().trim(),
            storeId = sp.getString(KEY_STORE_ID, d.storeId).orEmpty().trim(),
            storeName = sp.getString(KEY_STORE_NAME, d.storeName).orEmpty().trim(),
            fallbackText = sp.getString(KEY_FALLBACK_TEXT, d.fallbackText).orEmpty().trim(),
            deviceSerial = sp.getString(KEY_DEVICE_SERIAL, d.deviceSerial).orEmpty().trim(),
        )
    }

    fun save(context: Context, cfg: WorkerConfig) {
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_DECISION_URL, cfg.decisionUrl)
            .putString(KEY_DECISION_KEY, cfg.decisionKey)
            .putString(KEY_TENANT_ID, cfg.tenantId)
            .putString(KEY_STORE_ID, cfg.storeId)
            .putString(KEY_STORE_NAME, cfg.storeName)
            .putString(KEY_FALLBACK_TEXT, cfg.fallbackText)
            .putString(KEY_DEVICE_SERIAL, cfg.deviceSerial)
            .apply()
    }

    fun isAccessibilityEnabled(context: Context): Boolean {
        val component = "${context.packageName}/${QianFanAccessibilityService::class.java.name}"
        val enabled = try {
            Settings.Secure.getInt(context.contentResolver, Settings.Secure.ACCESSIBILITY_ENABLED, 0)
        } catch (_: Exception) {
            0
        }
        if (enabled != 1) return false
        val services = Settings.Secure.getString(
            context.contentResolver,
            Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
        ).orEmpty()
        return services.split(':').any { it.equals(component, ignoreCase = true) }
    }
}
