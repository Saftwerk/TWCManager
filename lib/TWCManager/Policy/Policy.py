import time
from ww import f
from termcolor import colored


class Policy:

    active_policy = None

    # This is the default charge policy.  It can be overridden or extended.
    charge_policy = [
        # The first policy table entry is for chargeNow. This will fire if
        # chargeNowAmps is set to a positive integer and chargeNowTimeEnd
        # is less than or equal to the current timestamp
        {
            "name": "Charge Now",
            "match": [
                "settings.chargeNowAmps",
                "settings.chargeNowTimeEnd",
                "settings.chargeNowTimeEnd",
            ],
            "condition": ["gt", "gt", "gt"],
            "value": [0, 0, "now"],
            "charge_amps": "settings.chargeNowAmps",
            "charge_limit": "config.chargeNowLimit",
        },
        # Check if we are currently within the Scheduled Amps charging schedule.
        # If so, charge at the specified number of amps.
        {
            "name": "Scheduled Charging",
            "match": ["checkScheduledCharging()"],
            "condition": ["eq"],
            "value": [1],
            "charge_amps": "settings.scheduledAmpsMax",
            "charge_limit": "config.scheduledLimit",
        },
        # If we are within Track Green Energy schedule, charging will be
        # performed based on the amount of solar energy being produced.
        # Don't bother to check solar generation before 6am or after
        # 8pm. Sunrise in most U.S. areas varies from a little before
        # 6am in Jun to almost 7:30am in Nov before the clocks get set
        # back an hour. Sunset can be ~4:30pm to just after 8pm.
        {
            "name": "Track Green Energy",
            "match": ["tm_hour", "tm_hour", "settings.hourResumeTrackGreenEnergy"],
            "condition": ["gte", "lt", "lte"],
            "value": [6, 20, "tm_hour"],
            "charge_amps": "getMaxAmpsToDivideGreenEnergy()",
            "background_task": "checkGreenEnergy",
            "allowed_flex": "config.greenEnergyFlexAmps",
            "charge_limit": "config.greenEnergyLimit",
        },
        # If all else fails (ie no other policy match), we will charge at
        # nonScheduledAmpsMax
        {
            "name": "Non Scheduled Charging",
            "match": ["none"],
            "condition": ["none"],
            "value": [0],
            "charge_amps": "settings.nonScheduledAmpsMax",
            "charge_limit": "config.nonScheduledLimit",
        },
    ]
    lastPolicyCheck = 0
    master = None
    policyCheckInterval = 30

    def __init__(self, master):
        self.master = master
        self.config = self.master.config

        # Override Charge Policy if specified
        config_policy = self.config.get("policy")
        if config_policy:
            if len(config_policy.get("override", [])) > 0:
                # Policy override specified, just override in place without processing the
                # extensions
                self.charge_policy = config_policy.get("override")
            else:
                # Insert optional policy extensions into policy list
                # After - Inserted before Non-Scheduled Charging
                config_extend = config_policy.get("extend", {})
                if len(config_extend.get("after", [])) > 0:
                    self.charge_policy[3:3] = config_extend.get("after")

                # Before - Inserted after Charge Now
                if len(config_extend.get("before", [])) > 0:
                    self.charge_policy[1:1] = config_extend.get("before")

                # Emergency - Inserted at the beginning
                if len(config_extend.get("emergency", [])) > 0:
                    self.charge_policy[0:0] = config_extend.get("emergency")

            # Set the Policy Check Interval if specified
            policy_engine = config_policy.get("engine")
            if policy_engine:
                if policy_engine.get("policyCheckInterval"):
                    self.policyCheckInterval = policy_engine.get("policyCheckInterval")

    def setChargingPerPolicy(self):
        # This function is called for the purpose of evaluating the charging
        # policy and matching the first rule which matches our scenario.

        # Once we have determined the maximum number of amps for all slaves to
        # share based on the policy, we call setMaxAmpsToDivideAmongSlaves to
        # distribute the designated power amongst slaves.

        # First, determine if it has been less than 30 seconds since the last
        # policy check. If so, skip for now
        if (self.lastPolicyCheck + self.policyCheckInterval) > time.time():
            return
        else:
            # Update last policy check time
            self.lastPolicyCheck = time.time()

        for policy in self.charge_policy:

            # Check if the policy is within its latching period
            latched = False
            if "__latchTime" in policy:
                if time.time() < policy["__latchTime"]:
                    latched = True
                else:
                    del policy["__latchTime"]

            # Iterate through each set of match, condition and value sets
            iter = 0
            for match, condition, value in zip(
                policy["match"], policy["condition"], policy["value"]
            ):

                if not latched:
                    iter += 1
                    self.master.debugLog(
                        8,
                        "Policy    ",
                        f(
                            "Evaluating Policy match ({colored(match, 'red')}), condition ({colored(condition, 'red')}), value ({colored(value, 'red')}), iteration ({colored(iter, 'red')})"
                        ),
                    )
                    # Start by not having matched the condition
                    is_matched = self.doesConditionMatch(match, condition, value)

                # Check if we have met all criteria
                if latched or is_matched:

                    # Have we checked all policy conditions yet?
                    if latched or len(policy["match"]) == iter:

                        # Yes, we will now enforce policy
                        self.master.debugLog(
                            7,
                            "Policy    ",
                            f(
                                "All policy conditions have matched. Policy chosen is {colored(policy['name'], 'red')}"
                            ),
                        )
                        if self.active_policy != str(policy["name"]):
                            self.master.debugLog(
                                1,
                                "Policy    ",
                                f(
                                    "New policy selected; changing to {colored(policy['name'], 'red')}"
                                ),
                            )
                            self.active_policy = str(policy["name"])

                        if not latched and "latch_period" in policy:
                            policy["__latchTime"] = (
                                time.time() + policy["latch_period"] * 60
                            )

                        # Determine which value to set the charging to
                        if policy["charge_amps"] == "value":
                            self.master.setMaxAmpsToDivideAmongSlaves(
                                int(policy["value"])
                            )
                            self.master.debugLog(
                                10,
                                "Policy    ",
                                "Charge at %.2f" % int(policy["value"]),
                            )
                        else:
                            self.master.setMaxAmpsToDivideAmongSlaves(
                                self.policyValue(policy["charge_amps"])
                            )
                            self.master.debugLog(
                                10,
                                "Policy    ",
                                "Charge at %.2f"
                                % self.policyValue(policy["charge_amps"]),
                            )

                        # Set flex, if any
                        self.master.setAllowedFlex(
                            self.policyValue(policy.get("allowed_flex", 0))
                        )

                        # If a background task is defined for this policy, queue it
                        bgt = policy.get("background_task", None)
                        if bgt:
                            self.master.queue_background_task({"cmd": bgt})

                        # If a charge limit is defined for this policy, apply it
                        limit = self.policyValue(policy.get("charge_limit", -1))
                        if not (limit >= 50 and limit <= 100):
                            limit = -1
                        self.master.queue_background_task(
                            {"cmd": "applyChargeLimit", "limit": limit}
                        )

                        # Now, finish processing
                        return

                    else:
                        self.master.debugLog(
                            8,
                            "Policy    ",
                            "This policy condition has matched, but there are more to process.",
                        )

                else:
                    self.master.debugLog(
                        8, "Policy    ", "Policy conditions were not matched."
                    )
                    break

    def policyValue(self, value):
        # policyValue is a macro to allow charging policy to refer to things
        # such as EMS module values or settings. This allows us to control
        # charging via policy.
        ltNow = time.localtime()

        # Anything other than a string can only be a literal value
        if not isinstance(value, str):
            return value

        # If value is "now", substitute with current timestamp
        if value == "now":
            return time.time()

        # If value is "tm_hour", substitute with current hour
        if value == "tm_hour":
            return ltNow.tm_hour

        # The remaining checks are case-sensitive!
        #
        # If value refers to a function, execute the function and capture the
        # output
        if value == "getMaxAmpsToDivideGreenEnergy()":
            return self.master.getMaxAmpsToDivideGreenEnergy()
        elif value == "checkScheduledCharging()":
            return self.master.checkScheduledCharging()

        # If value is tiered, split it up
        if value.find(".") != -1:
            pieces = value.split(".")

            # If value refers to a setting, return the setting
            if pieces[0] == "settings":
                return self.master.settings.get(pieces[1], 0)
            elif pieces[0] == "config":
                return self.config["config"].get(pieces[1], 0)
            elif pieces[0] == "modules":
                module = None
                if pieces[1] in self.master.modules:
                    module = self.master.getModuleByName(pieces[1])
                    return getattr(module, pieces[2], value)

        # None of the macro conditions matched, return the value as is
        return value

    def policyIsGreen(self):
        current_policy = next(
            policy
            for policy in self.charge_policy
            if policy["name"] == self.active_policy
        )

        return (
            True
            if current_policy.get("background_task", "") == "checkGreenEnergy"
            and current_policy.get("charge_amps", "")
            == "getMaxAmpsToDivideGreenEnergy()"
            else False
        )

    def doesConditionMatch(self, match, condition, value):
        match = self.policyValue(match)
        value = self.policyValue(value)

        # Perform comparison
        if condition == "gt":
            # Match must be greater than value
            return True if match > value else False
        elif condition == "gte":
            # Match must be greater than or equal to value
            return True if match >= value else False
        elif condition == "lt":
            # Match must be less than value
            return True if match < value else False
        elif condition == "lte":
            # Match must be less than or equal to value
            return True if match <= value else False
        elif condition == "eq":
            # Match must be equal to value
            return True if match == value else False
        elif condition == "ne":
            # Match must not be equal to value
            return True if match != value else False
        elif condition == "false":
            # Condition: false is a method to ensure a policy entry
            # is never matched, possibly for testing purposes
            return False
        elif condition == "none":
            # No condition exists.
            return True
        else:
            raise ValueError("Unknown condition " + condition)
