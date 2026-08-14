package com.dxl.kefu.qianfan

import android.content.Intent
import android.content.ComponentName
import android.os.Bundle
import android.provider.Settings
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity

class MainActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        val etDecisionUrl = findViewById<EditText>(R.id.etDecisionUrl)
        val etDecisionKey = findViewById<EditText>(R.id.etDecisionKey)
        val etTenantId = findViewById<EditText>(R.id.etTenantId)
        val etStoreId = findViewById<EditText>(R.id.etStoreId)
        val etStoreName = findViewById<EditText>(R.id.etStoreName)
        val etFallbackText = findViewById<EditText>(R.id.etFallbackText)
        val tvStatus = findViewById<TextView>(R.id.tvStatus)

        fun refreshStatus() {
            val ok = WorkerPrefs.isAccessibilityEnabled(this)
            tvStatus.text = if (ok) {
                "状态：无障碍已启用"
            } else {
                "状态：无障碍未启用"
            }
        }

        val cfg = WorkerPrefs.load(this)
        etDecisionUrl.setText(cfg.decisionUrl)
        etDecisionKey.setText(cfg.decisionKey)
        etTenantId.setText(cfg.tenantId)
        etStoreId.setText(cfg.storeId)
        etStoreName.setText(cfg.storeName)
        etFallbackText.setText(cfg.fallbackText)
        refreshStatus()

        findViewById<Button>(R.id.btnSave).setOnClickListener {
            val oldCfg = WorkerPrefs.load(this)
            val defaults = WorkerConfig.DEFAULT
            val newCfg = WorkerConfig(
                decisionUrl = etDecisionUrl.text.toString().trim(),
                decisionKey = etDecisionKey.text.toString().trim(),
                tenantId = etTenantId.text.toString().trim().ifBlank { defaults.tenantId },
                storeId = etStoreId.text.toString().trim().ifBlank { defaults.storeId },
                storeName = etStoreName.text.toString().trim().ifBlank { defaults.storeName },
                fallbackText = etFallbackText.text.toString().trim().ifBlank { defaults.fallbackText },
                deviceSerial = oldCfg.deviceSerial,
            )
            WorkerPrefs.save(this, newCfg)
            Toast.makeText(this, "配置已保存", Toast.LENGTH_SHORT).show()
            refreshStatus()
        }

        findViewById<Button>(R.id.btnOpenA11y).setOnClickListener {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        }

        findViewById<Button>(R.id.btnOpenQianFan).setOnClickListener {
            val pm = packageManager
            val launchIntent = pm.getLaunchIntentForPackage("com.xingin.eva")
            if (launchIntent != null) {
                startActivity(launchIntent)
                return@setOnClickListener
            }

            val explicit = Intent().apply {
                component = ComponentName(
                    "com.xingin.eva",
                    "com.xingin.eva.container.MainActivity",
                )
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            val ok = runCatching {
                startActivity(explicit)
                true
            }.getOrElse { false }
            if (!ok) {
                Toast.makeText(this, "未找到千帆App", Toast.LENGTH_SHORT).show()
            }
        }
    }

    override fun onResume() {
        super.onResume()
        findViewById<TextView>(R.id.tvStatus).text = if (WorkerPrefs.isAccessibilityEnabled(this)) {
            "状态：无障碍已启用"
        } else {
            "状态：无障碍未启用"
        }
    }
}
