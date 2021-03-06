# $language = "python"
# $interface = "1.0"

import os
import sys
import logging

# Add script directory to the PYTHONPATH so we can import our modules (only if run from SecureCRT)
if 'crt' in globals():
    script_dir, script_name = os.path.split(crt.ScriptFullName)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
else:
    script_dir, script_name = os.path.split(os.path.realpath(__file__))

# Now we can import our custom modules
from securecrt_tools import scripts
from securecrt_tools import sessions
from securecrt_tools import utilities
from s_vlan_to_csv import normalize_port_list
# Import message box constants names for use specifying the design of message boxes
from securecrt_tools.message_box_const import *

# Create global logger so we can write debug messages from any function (if debug mode setting is enabled in settings).
logger = logging.getLogger("securecrt")
logger.debug("Starting execution of {0}".format(script_name))


# ################################################   SCRIPT LOGIC   ##################################################

def script_main(script):
    """
    | MULTIPLE device script
    | Author: Jamie Caesar
    | Email: jcaesar@presidio.com

    This script will provide a list of all the switches that have locally connected MAC addresses in their mac address
    table for a range of VLANs.

    After launching the script, it will prompt for a CSV file with all of the devices the script should connect to.
    It will also prompt for a range of VLANs that it should look for MAC addresses
    This script checks that it will NOT be run in a connected tab.

    :param script: A subclass of the scripts.Script object that represents the execution of this particular script
                   (either CRTScript or DirectScript)
    :type script: scripts.Script
    """
    session = script.get_main_session()

    # If this is launched on an active tab, disconnect before continuing.
    logger.debug("<M_SCRIPT> Checking if current tab is connected.")
    if session.is_connected():
        logger.debug("<M_SCRIPT> Existing tab connected.  Stopping execution.")
        raise scripts.ScriptError("This script must be launched in a not-connected tab.")

    # Load a device list
    device_list = script.import_device_list()
    if not device_list:
        return

    # Check settings if we should use a proxy/jumpbox
    use_proxy = script.settings.getboolean("Global", "use_proxy")
    default_proxy_session = script.settings.get("Global", "proxy_session")

    num_string = script.prompt_window("Provide a list of VLANs to search the devices for (e.g. 1,2,5-7,9)",
                                      "Input range")
    if not num_string:
        return

    vlan_set = set(utilities.expand_number_range(num_string))

    # Create a filename to keep track of our connection logs, if we have failures.  Use script name without extension
    failed_log = session.create_output_filename("{0}-LOG".format(script_name.split(".")[0]), include_hostname=False)

    output_filename = session.create_output_filename("mac-search-by-vlan", include_hostname=False, ext=".txt")
    with open(output_filename, 'w') as output_file:
        output_file.write("MAC ADDRESS SEARCH IN VLANS: {0}\n\n".format(num_string))
        # ########################################  START DEVICE CONNECT LOOP  ###########################################
        for device in device_list:
            hostname = device['Hostname']
            protocol = device['Protocol']
            username = device['Username']
            password = device['Password']
            enable = device['Enable']
            try:
                proxy = device['Proxy Session']
            except KeyError:
                proxy = None

            if not proxy and use_proxy:
                proxy = default_proxy_session

            logger.debug("<M_SCRIPT> Connecting to {0}.".format(hostname))
            try:
                script.connect(hostname, username, password, protocol=protocol, proxy=proxy)
                session = script.get_main_session()
                hostname, matched_macs = per_device_work(session, enable, vlan_set)
                script.disconnect()
                if matched_macs:
                    output_file.write("### Device: {0} ###\n".format(hostname))
                    output_file.write("VLAN    MAC                  PORT\n")
                    for line in matched_macs:
                        output_line = line[0]
                        output_line += ' ' * (8 - len(line[0]))
                        output_line += line[1]
                        output_line += ' ' * (20 - len(line[1]))
                        output_line += line[2]
                        output_file.write("{}\n".format(output_line))
                    output_file.write("\n\n")
                    output_file.flush()
            except scripts.ConnectError as e:
                with open(failed_log, 'a') as logfile:
                    logfile.write("<M_SCRIPT> Connect to {0} failed: {1}\n".format(hostname, e.message.strip()))
                    session.disconnect()
            except sessions.InteractionError as e:
                with open(failed_log, 'a') as logfile:
                    logfile.write("<M_SCRIPT> Failure on {0}: {1}\n".format(hostname, e.message.strip()))
                    session.disconnect()
            except sessions.UnsupportedOSError as e:
                with open(failed_log, 'a') as logfile:
                    logfile.write("<M_SCRIPT> Unsupported OS on {0}: {1}\n".format(hostname, e.message.strip()))
                    session.disconnect()

    # #########################################  END DEVICE CONNECT LOOP  ############################################


def per_device_work(session, enable_pass, vlan_set):
    """
    This function contains the code that should be executed on each device that this script connects to.  It is called
    after establishing a connection to each device in the loop above.

    You can either put your own code here, or if there is a single-device version of a script that performs the correct
    task, it can be imported and called here, essentially making this script connect to all the devices in the chosen
    CSV file and then running a single-device script on each of them.
    """
    session.start_cisco_session()

    script = session.script
    hostname = session.hostname

    exclude_ports = {}
    # Find uplink port via spanning-tree root information, so we can exclude MAC addresses found on that port from
    # our analysis.
    if session.os == "IOS":
        template_file = script.get_template("cisco_os_show_spanning-tree_root.template")
    else:
        template_file = script.get_template("cisco_os_show_spanning-tree_root.template")

    raw_stp_root = session.get_command_output("show spanning-tree root")

    root_results = utilities.textfsm_parse_to_list(raw_stp_root, template_file, add_header=False)

    for entry in root_results:
        vlan_string = entry[0]
        vlan = int(vlan_string.split("N")[1])
        uplink = utilities.long_int_name(entry[6]).strip()
        if uplink:
            exclude_ports[vlan] = uplink

    # TextFSM template for parsing "show mac address-table" output
    if session.os == "NXOS":
        template_file = script.get_template("cisco_nxos_show_mac_addr_table.template")
    else:
        template_file = script.get_template("cisco_ios_show_mac_addr_table.template")

    raw_mac = session.get_command_output("show mac address-table")
    fsm_results = utilities.textfsm_parse_to_list(raw_mac, template_file, add_header=False)

    # Check if IOS mac_table is empty -- if so, it is probably because the switch has an older IOS
    # that expects "show mac-address-table" instead of "show mac address-table".
    if session.os == "IOS" and len(fsm_results) == 1:
        send_cmd = "show mac-address-table dynamic"
        logger.debug("Retrying with command set to '{0}'".format(send_cmd))
        raw_mac = session.get_command_output(send_cmd)
        fsm_results = utilities.textfsm_parse_to_list(raw_mac, template_file, add_header=False)

    # Find all MAC entries that are from the specific VLAN range and aren't learned from the uplink port for that VLAN
    results = []
    for entry in fsm_results:
        try:
            vlan = int(entry[0])
        except ValueError:
            continue

        if vlan in vlan_set:
            try:
                uplink = exclude_ports[vlan]
            except KeyError:
                uplink = ""

            mac_intf = utilities.long_int_name(entry[2]).lower()
            uplink_intf = uplink.strip().lower()
            if not mac_intf == uplink_intf:
                results.append(entry)

    # Return terminal parameters back to starting values
    session.end_cisco_session()

    return hostname, results


# ################################################  SCRIPT LAUNCH   ###################################################

# If this script is run from SecureCRT directly, use the SecureCRT specific class
if __name__ == "__builtin__":
    # Initialize script object
    crt_script = scripts.CRTScript(crt)
    # Run script's main logic against the script object
    script_main(crt_script)
    # Shutdown logging after
    logging.shutdown()

# If the script is being run directly, use the simulation class
elif __name__ == "__main__":
    # Initialize script object
    direct_script = scripts.DebugScript(os.path.realpath(__file__))
    # Run script's main logic against the script object
    script_main(direct_script)
    # Shutdown logging after
    logging.shutdown()