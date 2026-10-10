# import subprocess
# from devices import devices

# for device in devices:
#     print("Device:", device["name"])
# print("IP Address:", device["ip"])
# result = subprocess.run(
#     ["ping", "-c", "1", device["ip"]],
#     capture_output=True,
#     timeout=2
# )
# print()


#
import subprocess
from devices import devices
from datetime import datetime


#
print("================================")
print(" Network Monitoring Report")
print("================================")
print("Time:", datetime.now())
print()


#
online_devices = 0
offline_devices = 0
issues_detected = 0
latency_threshold = 100
packet_loss_threshold = 5


#
for device in devices:
    print("-------------------------")
    print("Device:", device["name"])
    print("IP Address:", device["ip"])


    #
    try:
        result = subprocess.run(
            ["ping", "-c", "1", device["ip"]],
            capture_output=True,
            text=True,
            timeout=2
        )

        #
        if result.returncode == 0:
            online_devices += 1
            print("Status: ONLINE")
        else:
            offline_devices += 1
            issues_detected += 1
            print("Status: OFFLINE")
            print("Assessment: DEVICE UNREACHABLE")


        # print(result.stdout) prints out the raw input
        latency = result.stdout.split("time=")[1].split(" ")[0]
        print("Latency:", latency, "ms")

        #
        if float(latency) <= latency_threshold:
            print("Connection: GOOD")
        else:
            print("Connection: HIGH LATENCY")
        packet_loss = result.stdout.split("% packet loss")[0].split()[-1]
        print("Packet Loss:", packet_loss, "%")

        #
        if float(packet_loss) == 0:
            print("Packet Loss Status: GOOD")
        else:
            print("Packet Loss Status: DETECTED")

        #
        # if float(packet_loss) > 0:
        #     issues_detected += 1
        #     print("Assessment: CONNECTION ISSUE")
        # elif float(latency) > latency_threshold:
        #     issues_detected += 1
        #     print("Assessment: HIGH LATENCY")
        # else:
        #     print("Assessment: HEALTHY")
        # the code for the earlier stage of v2

        # if float(packet_loss) > packet_loss_threshold:
        #     issues_detected += 1
        #     print("Assessment: HIGH PACKET LOSS")
        # elif float(latency) > latency_threshold:
        #      issues_detected += 1
        #      print("Assessment: HIGH LATENCY")
        # else:
        #      print("Assessment: HEALTHY")
        # also a more complex version
        if float(packet_loss) > packet_loss_threshold and float(latency) > latency_threshold:
            issues_detected += 1
            print("Assessment: DEGRADED CONNECTION")
        elif float(packet_loss) > packet_loss_threshold:
            issues_detected += 1
            print("Assessment: HIGH PACKET LOSS")
        elif float(latency) > latency_threshold:
            issues_detected += 1
            print("Assessment: HIGH LATENCY")
        else:
            print("Assessment: HEALTHY")

        #
    except subprocess.TimeoutExpired:
        offline_devices += 1
        issues_detected += 1
        print("Status: OFFLINE")
        print("Assessment: CONNECTION TIMEOUT")

    print()

print("=========================")
print("Monitoring Summary")
print("=========================")
print("Total Devices:", len(devices))
print("Online:", online_devices)
print("Offline:", offline_devices)
print("Issues Detected:", issues_detected)

if issues_detected == 0:
    print("Overall Status: HEALTHY")
else:
    print("Overall Status: ATTENTION REQUIRED")

    # You now have a program that can take a list of network devices and produce a useful status report
    # This is V1 of my project

