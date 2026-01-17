## 设备清单
记录当前连接的设备
通过 hdc tconn 连接
hdc list targets 查看

## 设备连接问题排查
```json
[
    {
        "device_id": "192.168.31.204:5555",
        "type": "phone",
        "model": "HUAWEI Mate 60",
        "udid": "AE95087B7ED5F1237FF6CEFC1B07EF69BF97DE3E13F31C88BCC4B678608879E9"
    },
    {
        "device_id": "192.168.31.12:5555",
        "type": "pc",
        "model": "HUAWEI MateBook Pro",
        "udid": "E63F2BC3D06D30B4D8DE1E37C7E3A8027C8DD1BF3E1F7C1487774E37E1B21E1D"
    },
    {
        "device_id": "192.168.31.133:5555",
        "type": "pad",
        "model": "HUAWEI MatePad Mini",
        "udid": "2E8DDC0EA0625AAFA9B251856304949332DE731D4882118714C1B4452B19E5A1"
    }
]
```


## 设备连接问题排查
如果设备无法连接，尝试以下步骤排查：
1. 尝试重新连接设备，使用 `hdc tconn <device_id>` 命令重新建立连接。
2. 如果连接不上，可能是ip变了，这些设备会自动连到同一局域网，所以只需要检查局域网中的所有设备，尝试连接
判断设备名：
hdc -t 192.168.31.204:5555 shell param get const.product.name
或者判断udid:
hdc -t 192.168.31.204:5555 shell bm get --udid
如果能连接上，说明ip变了，更新设备清单即可。
3. 如果仍然无法连接，则报错给开发人员进行进一步排查。
