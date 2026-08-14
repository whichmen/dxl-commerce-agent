package com.dxl.kefu.qianniu

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

const val ACTION_GUARD_SYNC_CONFIG = "com.dxl.kefu.qianniu.action.GUARD_SYNC_CONFIG"
private const val EXTRA_STORE_ID = "store_id"
private const val EXTRA_STORE_NAME = "store_name"
private const val EXTRA_DEVICE_SERIAL = "device_serial"
private const val EXTRA_DECISION_URL = "decision_url"
private const val CFG_TAG = "QNWkrCfg"

class GuardConfigReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        if (intent?.action != ACTION_GUARD_SYNC_CONFIG) return
        val oldCfg = WorkerPrefs.load(context)
        val newStoreId = intent.getStringExtra(EXTRA_STORE_ID).orEmpty().trim()
        val newStoreName = intent.getStringExtra(EXTRA_STORE_NAME).orEmpty().trim()
        val newDeviceSerial = intent.getStringExtra(EXTRA_DEVICE_SERIAL).orEmpty().trim()
        val newDecisionUrl = intent.getStringExtra(EXTRA_DECISION_URL).orEmpty().trim()
        val nextCfg = oldCfg.copy(
            storeId = newStoreId.ifBlank { oldCfg.storeId },
            storeName = newStoreName.ifBlank { oldCfg.storeName },
            deviceSerial = newDeviceSerial.ifBlank { oldCfg.deviceSerial },
            decisionUrl = newDecisionUrl.ifBlank { oldCfg.decisionUrl },
        )
        if (nextCfg == oldCfg) return
        WorkerPrefs.save(context, nextCfg)
        Log.i(
            CFG_TAG,
            "guard sync applied store_id='${nextCfg.storeId}' device_serial='${nextCfg.deviceSerial}' decision_url='${nextCfg.decisionUrl}'",
        )
    }
}
