from pydoover import ui


class DseEngineControllerUI:
    def __init__(self):
        self.is_working = ui.BooleanVariable("is_working", "We Working?")
        self.uptime = ui.DateTimeVariable("uptime", "Started")

        self.send_alert = ui.Action("send_alert", "Send message as alert", position=1)
        self.text_parameter = ui.TextParameter("test_message", "Put in a message")

        self.test_output = ui.TextVariable("test_output", "This is message we got")

        self.battery = ui.Submodule("battery", "Battery Module")
        self.battery_voltage = ui.NumericVariable(
            "voltage", "Battery Voltage", precision=2, ranges=[
                ui.Range("Low", 0, 10, ui.Colour.red),
                ui.Range("Normal", 10, 20, ui.Colour.green),
                ui.Range("High", 20, 30, ui.Colour.blue),
            ])

        self.battery_low_voltage_alert = ui.NumericParameter("low_voltage_alert", "Low Voltage Alert")
        self.battery_charge_mode = ui.StateCommand("charge_mode", "Charge Mode", user_options=[
            ui.Option("charge", "Charge"),
            ui.Option("discharge", "Discharge"),
            ui.Option("idle", "Idle")
        ])
        self.battery.add_children(self.battery_voltage, self.battery_low_voltage_alert, self.battery_charge_mode)

    def fetch(self):
        return self.is_working, self.uptime, self.send_alert, self.text_parameter, self.test_output, self.battery

    def update(self, is_working, voltage, uptime):
        self.is_working.update(is_working)
        self.uptime.update(uptime)
        self.battery_voltage.update(voltage)
