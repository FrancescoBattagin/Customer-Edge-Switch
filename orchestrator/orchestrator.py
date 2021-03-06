import grpc
import os
import random
import hashlib
import sys
import p4runtime_sh.shell as sh
from p4runtime_sh.shell import PacketIn
from p4runtime_sh.shell import PacketOut
import time
from scapy.all import *
import yaml
import threading
import inotify.adapters
import json, base64
import hmac, hashlib
import socket
# No need to import p4runtime_lib
# import p4runtime_lib.bmv2

controller_ip = '192.168.56.2'
key_port = 100
auth_port = 101
mac_to_be_filtered = '0a:00:27:00:00:20' #virtualbox mac to be filtered

policies_list = []
mac_addresses = {}
#keys = [{"imsi":"5021301234567894", "key":"6b86b273ff34fce19d6b804eff5a3f5747ada4eaa22f1d49c01e52ddb7875b4b", "count":1}, {...}, ...]
keys = []

#open_entry_history = [{"ip_dst":"10.0.0.3", "ip_src":"10.0.0.1", "port":80, "ether_src":"ff:ff:ff:ff:ff:ff", "te":table_entry}, {...}, ...]
open_entry_history = []

#strict_entry_history = [{"ip_dst":"10.0.0.3", "ip_src":"10.0.0.1", "dport":80, "sport":1298, "dstAddr":"ff:ff:ff:ff:ff:ff", egress_port":2 "te":table_entry}, {...}, ...]
strict_entry_history = []

#check if PolicyDB has been modified
def mod_detector():
    while True:
        i = inotify.adapters.Inotify()
        i.add_watch("../orchestrator/policiesDB.yaml")

        for event in i.event_gen(yield_nones=False):
            (_, type_names, path, filename) = event

            if "IN_CLOSE_WRITE" in event[1]: #type_names is a list
                print("[!] POLICYDB MODIFIED")
                mod_manager()

            #log:
            #print("PATH=[{}] FILENAME=[{}] EVENT_TYPES={}".format(path, filename, type_names))

#find out specific modifications per policy
def mod_manager():
    global policies_list
    global mac_addresses
    global strict_entry_history
    tmp = policies_list
    getPolicies()

    found = False

    for policy_tmp in tmp:
        for policy in policies_list:
            if policy.get("serviceName") == policy_tmp.get("serviceName"):
                found = True

                if policy.get("ip") != policy_tmp.get("ip"):
                    print("[!] IP_MODIFICATIONS")
                    print("[!] Editing policies IP...")
                    editIPPolicies(policy_tmp.get("ip"), policy.get("ip"), policy.get("port")) #also bidirectional entry

                if policy.get("port") != policy_tmp.get("port"):
                    print("[!] PORT_MODIFICATIONS")
                    print("[!] Editing policies Port...")
                    editPortPolicies(policy_tmp.get("ip"), policy.get("port"))

                if policy.get("protocol") != policy_tmp.get("protocol"):
                    print("[!] PROTOCOL_MODIFICATIONS")

                #UE checks
                #add -> no need to add entries
                for ue in policy.get("allowed_users"):
                    if ue not in policy_tmp.get("allowed_users"):
                        print("[!] UE_MODIFICATIONS_ADD")
                #del -> need to delete previous entries
                for ue in policy_tmp.get("allowed_users"):
                    if ue not in policy.get("allowed_users"):
                        print("[!] UE_MODIFICATIONS_DEL")
                        if ue.get("method") == "ip": #ip already available, no need to check mapping to find ip to be deleted
                            delUE(ue.get("user") , policy.get("ip"))
                            #del bi-directional entry
                            delUE(policy.get("ip"), ue.get("user"))

                        else: #imsi or token
                            stream = open("../orchestrator/ip_map.yaml", 'r')
                            mapping = yaml.safe_load(stream)
                            for service in mapping:
                                if service.get("serviceName") == policy.get("serviceName") and service.get("ip") == policy.get("ip") and str(service.get("port")) == str(policy.get("port")): #same service, ip and port
                                    for user in service.get("allowed_users"):
                                        if user.get("method") == ue.get("method") and user.get("user") == ue.get("user"):
                                            delUE(user.get("actual_ip"), policy.get("ip"))

                if policy.get("tee") != policy_tmp.get("tee"):
                    print("[!] TEE_MODIFICATIONS\n")

                if policy.get("fs_encr") != policy_tmp.get("fs_encr"):
                    print("[!] FS_ENCR_MODIFICATIONS\n")

                if policy.get("net_encr") != policy_tmp.get("net_encr"):
                    print("[!] NET_ENCR_MODIFICATIONS\n")

                if policy.get("sec_boot") != policy_tmp.get("sec_boot"):
                    print("[!] SEC_BOOT_MODIFICATIONS\n")

                break

        if not found:
            print("[!] Service not found")
            print("[!] Deleting service policies...\n")
            delPolicies(policy_tmp.get("ip"))

    print("[!] New policies_list: ")
    print(policies_list)

#del policies when service not found
def delPolicies(ip):
    global strict_entry_history
    for dictionary in strict_entry_history:
        if dictionary["ip_dst"] == ip:
            dictionary["te"].delete()
            strict_entry_history.remove(dictionary)

#edit service ip (also bidirectional entry)
def editIPPolicies(old_ip, new_ip, port):
    global strict_entry_history
    for dictionary in strict_entry_history:
        if dictionary["ip_dst"] == old_ip:
            dictionary["te"].delete()
            print("[!] Previous entry deleted\n")
            addEntry(dictionary["ip_src"], new_ip, dictionary["dport"], dictionary["sport"], dictionary["dstAddr"], dictionary["egress_port"])
            strict_entry_history.remove(dictionary)

        if dictionary["ip_src"] == old_ip:
            dictionary["te"].delete()
            print("[!] Previous entry deleted\n")
            addEntry(new_ip, dictionary["ip_dst"], dictionary["dport"], dictionary["sport"], dictionary["dstAddr"], dictionary["egress_port"])
            strict_entry_history.remove(dictionary)

#edit service port
def editPortPolicies(ip, new_port):
    global strict_entry_history
    for dictionary in strict_entry_history:
        if dictionary["ip_dst"] == ip:
            print(dictionary)
            dictionary["te"].delete()
            print("[!] Previous entry deleted\n")
            addEntry(dictionary["ip_src"], ip, new_port, dictionary["sport"], dictionary["dstAddr"], dictionary["egress_port"])
            strict_entry_history.remove(dictionary)

        if dictionary["ip_src"] == ip:
            dictionary["te"].delete()
            print("[!] Previous entry deleted\n")
            addEntry(ip, dictionary["ip_dst"], dictionary["dport"], new_port, dictionary["dstAddr"], dictionary["egress_port"])
            strict_entry_history.remove(dictionary)

#delete a policy (old service, user not allowed anymore)
def delUE(ue_ip, service_ip):
    global strict_entry_history
    for dictionary in strict_entry_history:
        if dictionary["ip_src"] == ue_ip and dictionary["ip_dst"] == service_ip:
            dictionary["te"].delete()
            print("[!] Previous entry deleted\n")
            strict_entry_history.remove(dictionary)

        if dictionary["ip_src"] == service_ip and dictionary["ip_dst"] == ue_ip:
            dictionary["te"].delete()
            print("[!] Previous entry deleted\n")
            strict_entry_history.remove(dictionary)

#add a new tmp "open" entry
def addOpenEntry(ip_src, ip_dst, port, ether_dst, egress_port, ether_src):
    global open_entry_history
    te = sh.TableEntry('my_ingress.forward')(action='my_ingress.ipv4_forward')
    te.match["hdr.ipv4.srcAddr"] = ip_src
    te.match["hdr.ipv4.dstAddr"] = ip_dst
    te.match["dst_port"] = str(port)
    te.action["dstAddr"] = ether_dst
    te.action["port"] = str(egress_port)
    te.priority = 1
    te.insert()
    inserted = time.time()
    print("[!] New open entry added\n")
    open_entry_history.append({"ip_dst":ip_dst, "ip_src":ip_src, "port":str(port), "ether_src":ether_src, "te":te})

    def entry_timeout(ip_dst, ip_src, port, ether_src):
        global open_entry_history
        print("[!] Countdown started\n")
        timeout = time.time() + 25.0 #25 sec or more
        while True:
            entry = {}
            found = False
            for dictionary in open_entry_history:
                if dictionary["ip_dst"] == ip_dst and dictionary["ip_src"] == ip_src and dictionary["port"] == str(port) and dictionary["ether_src"] == ether_src:
                    entry = dictionary
                    found = True

            #open entry has been deleted
            if not found:
                print("[!] Open entry has been deleted\n")
                break

            if timeout - time.time() <= 0.0:
                #delete open entry
                entry["te"].delete()
                open_entry_history.remove(entry)
                print("[!] Open entry deleted, timeout\n")
                break
        return

    open_entry_timeout = threading.Thread(target = entry_timeout, args = (ip_dst, ip_src, port, ether_src,)).start()

#add a new "strict" (sport -> microsegmentation) entry
def addEntry(ip_src, ip_dst, dport, sport, ether_dst, egress_port):
    te = sh.TableEntry('my_ingress.forward')(action='my_ingress.ipv4_forward')
    te.match["hdr.ipv4.srcAddr"] = ip_src
    te.match["hdr.ipv4.dstAddr"] = ip_dst
    te.match["src_port"] = str(sport)
    te.match["dst_port"] = str(dport)
    te.action["dstAddr"] = ether_dst
    te.action["port"] = str(egress_port)
    te.priority = 1
    te.insert()
    print("[!] New entry added\n")
    strict_entry_history.append({"ip_dst":ip_dst, "ip_src":ip_src, "dport":str(dport), "sport":str(sport), "dstAddr":ether_dst, "egress_port":egress_port, "te":te})

#update policies_list
def getPolicies():
    #policyDB as a yaml file
    #each policy is a tuple containing specific attributes
    global policies_list
    stream = open("../orchestrator/policiesDB.yaml", 'r')
    policies_list = yaml.safe_load(stream)

#if policyDB is managed as a true db
def getPoliciesDB(packet):
    global policies_list
    try:
        with connect(
            host="localhost",
            user=input("Enter your username: "),
            password=input("Enter your password: "),
            database="PolicyDB"
        ) as connection:
            print(connection)
            prepared_statement = "SELECT * FROM policies"
            with connection.cursor() as cursor:
                cursor.execute(prepared_statement)
                policies_list = cursor.fetchall()
            print(policies)

    except Error as e:
        print(e)

#look for policy and add new entries if found (when a packet is received)
def lookForPolicy(policyList, auth_dict, client_ip):
    global mac_addresses
    found = False

    service_ip = auth_dict["service_ip"]
    method = auth_dict["method"]
    authentication = auth_dict["authentication"]
    port = auth_dict["port"]
    protocol = auth_dict["protocol"]

    ether_src = mac_addresses[client_ip]
    ether_dst = mac_addresses[service_ip]

    for policy in policyList:
        if service_ip == policy.get("ip") and int(port) == policy.get("port") and protocol == policy.get("protocol"):
            for user in policy.get("allowed_users"):
                if method == "ip":
                    if user.get("method") == "ip" and user.get("user") == authentication:
                        found = True
                        addOpenEntry(authentication, service_ip, port, ether_dst, 2, ether_src) #substitute specific egress_port; 2 in my case
                        break
                else: #imsi or token
                    stream = open("../orchestrator/ip_map.yaml", 'r')
                    mapping = yaml.safe_load(stream)
                    for service in mapping:
                        if service.get("serviceName") == policy.get("serviceName") and service.get("ip") == policy.get("ip") and str(service.get("port")) == str(policy.get("port")): #same service, ip and port
                            for ue in service.get("allowed_users"):
                                if user.get("method") == ue.get("method") and ue.get("method") == method and user.get("user") == ue.get("user") and ue.get("user") == authentication: #same method and same id (imsi or token)
                                    found = True
                                    print("[!] Retrieved ip: " + ue.get("actual_ip"))
                                    addOpenEntry(ue.get("actual_ip"), policy.get("ip"), policy.get("port"), ether_dst, 2, ether_src)
                                    break
    if not found:
        #packet drop
        packet = None
        print("[!] Packet dropped\n\n\n")

#add new ip-mac entry to dictionary
def arpManagement(packet):
    global mac_addresses
    mac = packet.getlayer(Ether).src
    if mac != mac_to_be_filtered:
        ip = packet.getlayer(ARP).psrc
        print(ip + " has MAC " + mac + "\n")
        if ip not in mac_addresses:
            mac_addresses[ip] = mac
        print(mac_addresses)

#diffie-hellman key computation
def key_computation(p, g, A, imsi):
    global keys
    global mac_addresses
    found = False
    begin = time.time()
    print("begin")
    print(begin)
    for dictionary in keys:
        if dictionary["imsi"] == imsi:
            found = True
    if not found:
        b = random.randint(10,20)
        B = (int(g)**int(b)) % int(p)
        print("B: " + str(B))
        keyB = hashlib.sha256(str((int(A)**int(b)) % int(p)).encode()).hexdigest()
        keys.append({"imsi":imsi, "key":keyB, "count":0})
        return B
    else:
        print("[!] This imsi has already a private key\n")

#handle a just received packet
def packetHandler(streamMessageResponse):
    global mac_addresses
    global keys
    packet = streamMessageResponse.packet

    if streamMessageResponse.WhichOneof('update') =='packet':
        packet_payload = packet.payload
        pkt = Ether(_pkt=packet.payload)

        pkt_ether = pkt.getlayer(Ether)
        if pkt_ether != None:
            ether_src = pkt.getlayer(Ether).src
            ether_dst = pkt.getlayer(Ether).dst
        else:
            print("[!] Ether layer not present")

        if pkt.getlayer(IP) != None:
            pkt_src = pkt.getlayer(IP).src
            pkt_dst = pkt.getlayer(IP).dst

        if pkt.getlayer(TCP) != None:
            sport = pkt.getlayer(TCP).sport
            dport = pkt.getlayer(TCP).dport

        pkt_icmp = pkt.getlayer(ICMP)
        pkt_ip = pkt.getlayer(IP)
        pkt_arp = pkt.getlayer(ARP)
        pkt_udp = pkt.getlayer(UDP)

        reply = False
        #check for waited replies in open_entry_history
        for dictionary in open_entry_history:
            if pkt.getlayer(IP) != None and pkt_src == dictionary["ip_dst"] and pkt_dst == dictionary["ip_src"]:
                if pkt.getlayer(TCP) != None:
                    if str(pkt.getlayer(TCP).sport) == dictionary["port"]:
                        reply = True
                        print("[!] Reply arrived\n")
                        #add strict entries
                        addEntry(pkt_src, pkt_dst, pkt.getlayer(TCP).dport, dictionary["port"], dictionary["ether_src"], 1)
                        addEntry(pkt_dst, pkt_src, dictionary["port"], pkt.getlayer(TCP).dport, ether_src, 2)
                        #delete open entry
                        dictionary["te"].delete()
                        print("[!] Open entry deleted\n")
                        open_entry_history.remove(dictionary)

        if not reply:
            if pkt_icmp != None and pkt_ip != None and str(pkt_icmp.getlayer(ICMP).type) == "8":
                print("[!] Ping from: " + pkt_src)
                print("[!] ICMP layer not supported in p4 switch, not used\n")
            elif pkt_arp != None:
                print("[!] ARP info\n")
                arpManagement(pkt)
            elif pkt_ip != None:
                print("[!] Packet received: " + pkt_src + " --> " + pkt_dst + "\n")
                if pkt.getlayer(TCP) != None:
                    print("sport: " + str(pkt.getlayer(TCP).sport))
                    print("dport: " + str(pkt.getlayer(TCP).dport) + "\n")
            else:
                print("[!] No needed layers\n")

#setup connection w/ switch, sets policies_list, starts mod_detector thread and listens for new packets
def controller():
    global policies_list

    #connection
    sh.setup(
        device_id=1,
        grpc_addr='127.0.0.1:50051', #substitute ip and port with the ones of the specific switch
        election_id=(1, 0), # (high, low)
        config=sh.FwdPipeConfig('../p4/p4-test.p4info.txt','../p4/p4-test.json')
    )

    #deletion of already-present entries
    print("[!] Entries initial deletion")
    for te in sh.TableEntry("my_ingress.forward").read():
        te.delete()

    #get and save policies_list
    getPolicies()

    #thread that checks for policies modifications
    print("[!] Policies modifications detector started\n")
    detector = threading.Thread(target = mod_detector)
    detector.start()

    #thread that listens for auth connection
    def auth_thread():
        global auth_port
        host = "0.0.0.0"
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind((host, auth_port))
        s.listen()
        while True:
            connection_auth, client_address_auth = s.accept()
            with connection_auth:
                data = connection_auth.recv(1024)
                if not data:
                    "nothing"
                else:
                    print("Auth pkt received at: " + str(time.time()))
                    pkt_raw = str(data).split("---")
                    hmac_hex = pkt_raw[1][:-1] #remove '
                    auth = pkt_raw[0][2:] #remove b"
                    auth_bytes = base64.b64decode(auth[2:-1]) #remove b'
                    auth_string = auth_bytes.decode('unicode_escape')

                    def hmac_check(auth_string, auth_bytes, hmac_hex):
                        auth_dict = json.loads(auth_string)
                        imsi = auth_dict["imsi"]
                        count = auth_dict["count"]
                        service_ip = auth_dict["service_ip"]
                        if service_ip in mac_addresses:
                            found = False
                            for dictionary in keys:
                                if dictionary["imsi"] == imsi and dictionary["count"] < count:
                                    found = True
                                    key = dictionary["key"]
                                    dictionary["count"] = count
                                    base64_bytes = base64.b64encode(auth_bytes)
                                    hmac_hex_new = hmac.new(bytes(key, 'utf-8'), base64_bytes, hashlib.sha512).hexdigest()
                                    if hmac_hex_new == hmac_hex:
                                        print("[!] HMAC is the same! Looking for policies...\n")
                                        lookForPolicy(policies_list, auth_dict, client_address_auth[0])
                                    else:
                                        print("[!] HMAC is different. R u a thief?\n")
                                        break
                            if not found:
                                print("[!] User has not negotiated key yet\n")
                        else:
                            print("[!] service MAC is not known; still waiting for a gratuitous ARP\n")
                    hmac_check(auth_string, auth_bytes, hmac_hex)
                    return
    threading.Thread(target = auth_thread).start()

    def dh_thread():
        global key_port
        host = "0.0.0.0"
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind((host, key_port))
        s.listen()
        while True:
            connection, client_address = s.accept()
            with connection:
                data = connection.recv(1024)
                data = str(data)[2:-1] #remove b' and '
                dh = json.loads(data)
                p = dh["p"]
                g = dh['g']
                A = dh['A']
                imsi = dh['imsi']

                if dh['version'] == 1.0: #version
                    B = key_computation(p, g, A, imsi)
                    connection.send(bytes(str(B), 'utf-8'))
                return

    threading.Thread(target = dh_thread).start()

    #listening for new packets
    packet_in = sh.PacketIn()
    threads = []
    while True:
        
        def handle_thread_pkt_management(packet, threads):
            print("[!] Packet received\n")
            packet_handler = threading.Thread(target = packetHandler, args = (packet,))
            threads.append(packet_handler)
            packet_handler.start()
            print("[!] packet_handler started\n")
            for thread in threads:
                thread.join()

        packet_in.sniff(lambda m: handle_thread_pkt_management(m, threads), timeout = 0.01)

if __name__ == '__main__':
    controller()