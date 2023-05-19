"""
Battery Prediction app
see Readme for information
"""
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
from datetime import datetime, timedelta
import math
import re
import time
import pytz
import appdaemon.plugins.hass.hassapi as hass
import requests

TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
TIME_FORMAT_SECONDS = "%Y-%m-%dT%H:%M:%S.%f%z"
TIME_FORMAT_OCTOPUS = "%Y-%m-%d %H:%M:%S%z"
MAX_CHARGE_LIMITS = 16
SIMULATE = False        # Debug option, when set don't write to entities but simulate each 30 min period
SIMULATE_LENGTH = 23*60 # How many periods to simulate, set to 0 for just current

class Inverter():
    def __init__(self, base, id=0):
        self.id = id
        self.base = base
        self.charge_enable = False
        self.charge_enable_time = False
        self.charge_start_time_minutes = self.base.forecast_minutes 
        self.charge_start_end_minutes = self.base.forecast_minutes 
        self.sim_charge_start_time = "00:00:00"
        self.sim_charge_end_time = "00:00:00"
        self.charge_window = []
        self.discharge_window = []
        self.discharge_enable = []
        self.current_charge_limit = 0
        self.soc_kw = 0

        # Battery size, charge and discharge rates
        self.soc_max = float(self.base.get_arg('soc_max', default=0, index=self.id)) * self.base.battery_scaling
        self.charge_rate = float(self.base.get_arg('charge_rate', attribute='max', index=self.id)) / 1000.0 / 60.0
        self.discharge_rate = float(self.base.get_arg('discharge_rate', attribute='max', index=self.id)) / 1000.0 / 60.0
        reserve_percent = float(self.base.get_arg('reserve', default=0, index=self.id))
        self.reserve = self.base.dp2(self.soc_max * reserve_percent / 100.0)

        self.base.log("New Inverter {} with soc_max {} charge_rate {} kw discharge_rate kw {} reserve {}".format(self.id, self.soc_max, self.base.dp2(self.charge_rate*60.0), self.base.dp2(self.discharge_rate*60.0), self.reserve))
        
    def update_status(self, minutes_now):
        self.charge_enable = self.base.get_arg('charge_enable', default = False, index=self.id)
        self.charge_enable_time = self.base.get_arg('scheduled_charge_enable', 'on', index=self.id) == 'on'
        self.soc_kw = float(self.base.get_arg('soc_kw', default=0, index=self.id)) * self.base.battery_scaling

        # If the battery is being charged then find the charge window
        if self.charge_enable and self.charge_enable_time:
            # Find current charge window
            if SIMULATE:
                charge_start_time = datetime.strptime(self.sim_charge_start_time, "%H:%M:%S")
                charge_end_time = datetime.strptime(self.sim_charge_end_time, "%H:%M:%S")
            else:
                charge_start_time = datetime.strptime(self.base.get_arg('charge_start_time', index=self.id), "%H:%M:%S")
                charge_end_time = datetime.strptime(self.base.get_arg('charge_end_time', index=self.id), "%H:%M:%S")

            # Compute charge window minutes start/end just for the next charge window
            self.charge_start_time_minutes = charge_start_time.hour * 60 + charge_start_time.minute
            self.charge_end_time_minutes = charge_end_time.hour * 60 + charge_end_time.minute

            if self.charge_end_time_minutes < self.charge_start_time_minutes:
                # As windows wrap, if end is in the future then move start back, otherwise forward
                if self.charge_end_time_minutes > minutes_now:
                    self.charge_start_time_minutes -= 60 * 24
                else:
                    self.charge_end_time_minutes += 60 * 24
        else:
            # If charging is disabled set a fake window outside
            self.charge_start_time_minutes = self.base.forecast_minutes
            self.charge_end_time_minutes = self.base.forecast_minutes

        # Construct charge window from the GivTCP settings
        self.charge_window = []

        self.base.log("Inverter {} scheduled charge enable is {}".format(self.id, self.base.get_arg('scheduled_charge_enable', 'on', index=self.id)))
        if self.charge_enable_time:
            minute = max(0, self.charge_start_time_minutes)  # Max is here is start could be before midnight now
            minute_end = self.charge_end_time_minutes
            while minute < self.base.forecast_minutes:
                window = {}
                window['start'] = minute
                window['end']   = minute_end
                self.charge_window.append(window)
                minute += 24 * 60
                minute_end += 24 * 60

        self.base.log('Inverter {} charge windows currently {}'.format(self.id, self.charge_window))

        # Work out existing charge limits and percent
        if self.charge_enable and self.charge_enable_time:
            self.current_charge_limit = float(self.base.get_arg('charge_limit', index=self.id))
        else:
            self.current_charge_limit = 0

        if self.charge_enable_time:
            self.base.log("Inverter {} Charge settings: {}-{} limit {} power {} kw".format(self.id, self.base.time_abs_str(self.charge_start_time_minutes), self.base.time_abs_str(self.charge_end_time_minutes), self.current_charge_limit, self.charge_rate*60.0))
        else:
            self.base.log("Inverter {} Charge settings: timed charged is disabled, power {} kw".format(self.id, self.charge_rate*60.0))
            
        # Construct discharge window from GivTCP settings (How? XXX)
        self.discharge_window = []

        # Pre-fill best discharge enable with Off
        self.discharge_enable = [False for i in range(0, len(self.discharge_window))]

    def adjust_battery_target(self, soc):
        """
        Adjust the battery charging target SOC % in GivTCP
        """
        # Check current setting and adjust
        if SIMULATE:
            current_soc = self.sim_soc
        else:
            current_soc = float(self.base.get_state(entity_id = self.base.get_arg('soc_percent', indirect=False, index=self.id), default=100))

        if current_soc != soc:
            self.base.log("Inverter {} Current SOC is {} and new target is {}".format(self.id, current_soc, soc))
            entity_soc = self.base.get_entity(self.base.get_arg('soc_percent', indirect=False, index=self.id))
            if entity_soc:
                if SIMULATE:
                    self.sim_soc = soc
                else:
                    entity_soc.call_service("set_value", value=soc)
                    if self.base.get_arg('set_soc_notify', False):
                        self.base.call_service("notify/notify", message='Predbat: Inverter {} Target SOC has been changed to {} at {}'.format(self.id, soc, self.base.time_now_str()))
                self.base.record_status("Inverter {} set soc to {} at {}".format(self.id, soc, self.base.time_now_str()))
            else:
                self.base.log("WARN: Inverter {} Unable to get entity to set SOC target".format(self.id))
        else:
            self.base.log("Inverter {} Current SOC is {} already at target".format(self.id, current_soc))

    def write_and_poll_option(self, name, entity, new_value):
        """
        GivTCP Workaround, keep writing until correct
        """
        old_value = ""
        tries = 12
        while old_value != new_value and tries > 0:
            entity.call_service("select_option", option=new_value)
            time.sleep(5)
            old_value = entity.get_state()
            tries -=1
        if tries == 0:
            self.base.log("WARN: Inverter {} Trying to write {} to {} didn't complete".format(self.id, name, new_value))

    def adjust_force_discharge(self, force_discharge, new_start_time=None, new_end_time=None):
        """
        Adjust force discharge on/off
        """
        if SIMULATE:
            old_inverter_mode = self.sim_inverter_mode
            old_start = self.sim_discharge_start
            old_end = self.sim_discharge_end
        else:
            old_inverter_mode = self.base.get_arg('inverter_mode', index=self.id)
            old_start = self.base.get_arg('discharge_start_time', index=self.id)
            old_end = self.base.get_arg('discharge_end_time', index=self.id)

        if force_discharge:
            new_inverter_mode = 'Timed Export'
        else:
            new_inverter_mode = 'Eco'

        # Start time to correct format
        if new_start_time:
            new_start = new_start_time.strftime("%H:%M:%S")
        else:
            new_start = None

        # End time to correct format
        if new_end_time:
            new_end = new_end_time.strftime("%H:%M:%S")
        else:
            new_end = None

        self.base.log("Inverter {} Adjust force discharge to {} times {} - {}, current mode {} times {} - {}".format(self.id, new_inverter_mode, new_start, new_end, old_inverter_mode, old_start, old_end))

        # Change start time
        if new_start and new_start != old_start:
            entity_discharge_start_time = self.base.get_entity(self.base.get_arg('discharge_start_time', indirect=False, index=self.id))
            self.base.log("Inverter {} set new start time on {} to {}".format(self.id, entity_discharge_start_time, new_start))
            self.base.record_status("Inverter {} set discharge start time to {} at {}".format(self.id, new_start, self.base.time_now_str()))
            if SIMULATE:
                self.sim_discharge_start = new_start
            else:
                self.write_and_poll_option("discharge_start_time", entity_discharge_start_time, new_start)

        # Change end time
        if new_end and new_end != old_end:
            entity_discharge_end_time = self.base.get_entity(self.base.get_arg('discharge_end_time', indirect=False, index=self.id))
            self.base.log("Inverter {} Set new end time on {} to {} was {}".format(self.id, entity_discharge_end_time, new_end, old_end))                    
            self.base.record_status("Inverter {} Set discharge end time to {} at {}".format(self.id, new_end, self.base.time_now_str()))
            if SIMULATE:
                self.sim_discharge_end = new_end
            else:
                self.write_and_poll_option("discharge_end_time", entity_discharge_end_time, new_end)

        # Change inverter mode
        if old_inverter_mode != new_inverter_mode:
            if SIMULATE:
                self.sim_inverter_mode = new_inverter_mode
            else:
                # Inverter mode
                entity_inverter_mode = self.base.get_entity(self.base.get_arg('inverter_mode', indirect=False, index=self.id))
                entity_inverter_mode.call_service("select_option", option=new_inverter_mode)

                # Notify
                if self.base.get_arg('set_discharge_notify', False):
                    self.base.call_service("notify/notify", message="Predbat: Inverter {} Force discharge set to {} at time {}".format(self.id, force_discharge, self.base.time_now_str()))

            self.base.record_status("Inverter {} Set discharge mode to {} at {}".format(self.id, new_inverter_mode, self.base.time_now_str()))
            self.base.log("Inverter {} Changing force discharge to {}".format(self.id, force_discharge))

    def disable_charge_window(self):
        """
        Disable charge window
        """
        if SIMULATE:
            old_charge_schedule_enable = 'off'
        else:
            old_charge_schedule_enable = self.base.get_arg('scheduled_charge_enable', 'on', index=self.id)

        if old_charge_schedule_enable == 'on':
            if not SIMULATE:
                # Enable scheduled charge if not turned on
                entity_start = self.base.get_entity(self.base.get_arg('scheduled_charge_enable', indirect=False, index=self.id))
                entity_start.call_service("turn_off")
                if self.base.get_arg('set_soc_notify', False):
                    self.base.call_service("notify/notify", message="Predbat: Inverter {} Disabled scheduled charging at {}".format(self.id, self.base.time_now_str()))
            else:
                self.sim_charge_schedule_enable = False

            # Updated cached status to disabled    
            self.charge_enable_time = False
            self.charge_start_time_minutes = self.base.forecast_minutes
            self.charge_end_time_minutes = self.base.forecast_minutes

            self.base.record_status("Inverter {} Turned off scheduled charge".format(self.id))
            self.base.log("Inverter {} Turning off scheduled charge".format(self.id))

    def adjust_charge_window(self, charge_start_time, charge_end_time):
        """
        Adjust the charging window times (start and end) in GivTCP
        """
        if SIMULATE:
            old_start = self.sim_charge_start_time
            old_end = self.sim_charge_end_time
            old_charge_schedule_enable = self.sim_charge_schedule_enable
        else:
            old_start = self.base.get_arg('charge_start_time', index=self.id)
            old_end = self.base.get_arg('charge_end_time', index=self.id)
            old_charge_schedule_enable = self.base.get_arg('scheduled_charge_enable', True, index=self.id)

        new_start = charge_start_time.strftime("%H:%M:%S")
        new_end = charge_end_time.strftime("%H:%M:%S")

        self.base.log("Inverter {} charge window is {} - {}, being changed to {} - {}".format(self.id, old_start, old_end, new_start, new_end))

        if old_charge_schedule_enable == 'off':
            if not SIMULATE:
                # Enable scheduled charge if not turned on
                entity_start = self.base.get_entity(self.base.get_arg('scheduled_charge_enable', indirect=False, index=self.id))
                entity_start.call_service("turn_on")
                if self.base.get_arg('set_soc_notify', False):
                    self.base.call_service("notify/notify", message="Predbat: Inverter {} Enabling scheduled charging at {}".format(self.id, self.base.time_now_str()))
            else:
                self.sim_charge_schedule_enable = True

            self.charge_enable_time = True
            self.base.record_status("Inverter {} Turned on charge enable".format(self.id))
            self.base.log("Inverter {} Turning on scheduled charge".format(self.id))

        # Program start slot
        if new_start != old_start:
            if SIMULATE:
                self.sim_charge_start_time = new_start
                self.base.log("Simulate sim_charge_start_time now {}".format(new_start))
            else:
                entity_start = self.base.get_entity(self.base.get_arg('charge_start_time', indirect=False, index=self.id))
                # entity_start.call_service("select_option", option=new_start)
                self.write_and_poll_option("charge_start_time", entity_start, new_start)

        # Program end slot
        if new_end != old_end:
            if SIMULATE:
                self.sim_charge_end_time = new_end
                self.log("Simulate sim_charge_end_time now {}".format(new_end))
            else:
                entity_end = self.base.get_entity(self.base.get_arg('charge_end_time', indirect=False, index=self.id))
                self.write_and_poll_option("charge_end_time", entity_end, new_end)
                # entity_end.call_service("select_option", option=new_end)

        if new_start != old_start or new_end != old_end:
            if self.base.get_arg('set_window_notify', False) and not SIMULATE:
                self.base.call_service("notify/notify", message="Predbat: Inverter {} Charge window change to: {} - {} at {}".format(self.id, new_start, new_end, self.base.time_now_str()))
            self.base.record_status("Inverter {} Charge window change to: {} - {} at {}".format(self.id, new_start, new_end, self.base.time_now_str()))
            self.base.log("Inverter {} Updated start and end charge window to {} - {} (old {} - {})".format(self.id, new_start, new_end, old_start, old_end))


class PredBat(hass.Hass):
    """ 
    The battery prediction class itself 
    """

    def resolve_arg(self, arg, value, default=None, indirect=True, combine=False, attribute=None, index=None):
        """
        Resolve argument templates and state instances
        """
        if isinstance(value, list) and (index is not None):
            if index < len(value):
                value = value[index]
            else:
                self.log("WARN: Out of range index {} within item {} value {}".format(index, arg, value))
                value = None
            index = None

        if index:
            self.log("WARN: Out of range index {} within item {} value {}".format(index, arg, value))

        # If we have a list of items get each and add them up or return them as a list
        if isinstance(value, list):
            if combine:
                final = 0
                for item in value:
                    got = self.resolve_arg(arg, item, default=default, indirect=True)
                    try:
                        final += float(got)
                    except ValueError:
                        self.log("WARN: Return bad value {} from {} arg {}".format(got, item, arg))
                return final
            else:
                final = []
                for item in value:
                    item = self.resolve_arg(arg, item, default=default, indirect=indirect)
                    final.append(item)
                return final

        # Resolve templated data
        for repeat in range(0, 2):
            if isinstance(value, str) and '{' in value:
                value = value.format(**self.args)

        # Resolve indirect instance
        if indirect and isinstance(value, str) and '.' in value:
            if attribute:
                value = self.get_state(entity_id = value, default=default, attribute=attribute)
            else:
                value = self.get_state(entity_id = value, default=default)
        return value

    def get_arg(self, arg, default=None, indirect=True, combine=False, attribute=None, index=None):
        """
        Argument getter that can use HA state as well as fixed values
        """
        value = self.args.get(arg, default)
        value = self.resolve_arg(arg, value, default=default, indirect=indirect, combine=combine, attribute=attribute, index=index)
        return value

    def download_octopus_rates(self, url):
        """
        Download octopus rates directly from a URL
        Retry 3 times and then throw error
        """
        for retry in range(0, 3):
            pdata = self.download_octopus_rates_func(url)
            if pdata:
                break
        if not pdata:
            self.log("Unable to download Octopus data from URL {}".format(url))
            raise ValueError
        return pdata

    def download_octopus_rates_func(self, url):
        """
        Download octopus rates directly from a URL
        """
        mdata = []

        for page in range(1, 4):
            r = requests.get(url + "?page={}".format(page))
            try:
                data = r.json()       
            except requests.exceptions.JSONDecodeError:
                self.log("WARN: Error downloading Octopus data from url {}".format(url))
                return {}
            mdata += data['results']
        pdata = self.minute_data(mdata, 2, self.midnight_utc, 'value_inc_vat', 'valid_from', backwards=False, to_key='valid_to')
        return pdata

    def mintes_to_time(self, updated, now):
        """
        Compute the number of minutes between a time (now) and the updated time
        """
        timeday = updated - now
        minutes = int(timeday.seconds / 60) + int(timeday.days * 60*24)
        return minutes

    def str2time(self, str):
        if '.' in str:
            tdata = datetime.strptime(str, TIME_FORMAT_SECONDS)
        else:
            tdata = datetime.strptime(str, TIME_FORMAT)
        return tdata

    def minute_data_import_export(self, now_utc, key):
        """
        Download one or more entities for import/export data
        """
        entity_ids = self.get_arg(key, indirect=False)
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]

        import_today = {}    
        for entity_id in entity_ids:
            import_today = self.minute_data(self.get_history(entity_id = entity_id, days = 2)[0], 
                                                2, now_utc, 'state', 'last_updated', backwards=True, smoothing=True, clean_increment=True, accumulate=import_today)
        return import_today

    def minute_data_load(self, now_utc):
        """
        Download one or more entities for load data
        """
        entity_ids = self.get_arg('load_today', indirect=False)
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]

        load_minutes = {}
        for entity_id in entity_ids:
            load_minutes = self.minute_data(self.get_history(entity_id = entity_id, days = self.days_previous + 1)[0], 
                                            self.days_previous + 1, now_utc, 'state', 'last_updated', backwards=True, smoothing=True, scale=self.get_arg('load_scaling', 1.0), clean_increment=True, accumulate=load_minutes)
        return load_minutes

    def minute_data(self, history, days, now, state_key, last_updated_key,
                    backwards=False, to_key=None, smoothing=False, clean_increment=False, divide_by=0, scale=1.0, accumulate=[]):
        """
        Turns data from HA into a hash of data indexed by minute with the data being the value
        Can be backwards in time for history (N minutes ago) or forward in time (N minutes in the future)
        """
        mdata = {}
        newest_state = 0
        last_state = 0
        newest_age = 99999
        prev_last_updated_time = None

        # Check history is valid
        if not history:
            self.log("Warning, empty history passed to minute_data, ignoring (check your settings)...")
            return mdata

        # Process history
        for item in history:

            # Ignore data without correct keys
            if state_key not in item:
                continue
            if last_updated_key not in item:
                continue

            # Unavailable or bad values
            if item[state_key] == 'unavailable' or item[state_key] == 'unknown':
                continue

            # Get the numerical key and the timestamp and ignore if in error
            try:
                state = float(item[state_key]) * scale
                last_updated_time = self.str2time(item[last_updated_key])
            except ValueError:
                continue

            # Divide down the state if required
            if divide_by:
                state /= divide_by
            
            # Update prev to the first if not set
            if not prev_last_updated_time:
                prev_last_updated_time = last_updated_time
                last_state = state

            # Work out end of time period
            # If we don't get it assume it's to the previous update, this is for historical data only (backwards)
            if to_key:
                to_time = self.str2time(item[to_key])
            else:
                if backwards:
                    to_time = prev_last_updated_time
                else:
                    to_time = None

            if backwards:
                timed = now - last_updated_time
                if to_time:
                    timed_to = now - to_time
            else:
                timed = last_updated_time - now
                if to_time:
                    timed_to = to_time - now

            minutes = int(timed.seconds / 60) + int(timed.days * 60*24)
            if to_time:
                minutes_to = int(timed_to.seconds / 60) + int(timed_to.days * 60*24)

            if minutes < newest_age:
                newest_age = minutes
                newest_state = state

            if to_time:
                minute = minutes
                if minute == minutes_to:
                    mdata[minute] = state
                else:
                    if smoothing:
                        # Reset to zero?
                        if state < last_state and (state == 0.0):
                            while minute < minutes_to:
                                mdata[minute] = state
                                minute += 1
                        else:
                            # Can't really go backwards as incrementing data
                            if state < last_state:
                                state = last_state
                            # Create linear function
                            diff = (state - last_state) / (minutes_to - minute)
                            index = 0
                            while minute < minutes_to:
                                mdata[minute] = state - diff*index
                                minute += 1
                                index += 1
                    else:
                        while minute < minutes_to:
                            mdata[minute] = state
                            minute += 1
            else:
                mdata[minutes] = state

            # Store previous time & state
            prev_last_updated_time = last_updated_time
            last_state = state

        # If we only have a start time then fill the gaps with the last values
        if not to_key:
            state = newest_state
            for minute in range(0, 60*24*days):
                rindex = 60*24*days - minute - 1
                state = mdata.get(rindex, state)
                mdata[rindex] = state
                minute += 1

        # Reverse data with smoothing 
        if clean_increment:
            mdata = self.clean_incrementing_reverse(mdata)

        # Accumulate to previous data?
        if accumulate:
            for minute in range(0, 60*24*days):
                if minute in mdata:
                    mdata[minute] += accumulate.get(minute, 0)
                else:
                    mdata[minute] = accumulate.get(minute, 0)

        return mdata

    def minutes_since_yesterday(self, now):
        """
        Calculate the number of minutes since 23:59 yesterday
        """
        yesterday = now - timedelta(days=1)
        yesterday_at_2359 = datetime.combine(yesterday, datetime.max.time())
        difference = now - yesterday_at_2359
        difference_minutes = int((difference.seconds + 59) / 60)
        return difference_minutes

    def dp2(self, value):
        """
        Round to 2 decimal places
        """
        return math.ceil(value*100)/100

    def dp3(self, value):
        """
        Round to 3 decimal places
        """
        return math.ceil(value*1000)/1000

    def in_charge_window(self, charge_window, minute_abs):
        """
        Work out if this minute is within the a charge window
        """
        window_n = 0
        for window in charge_window:
            if minute_abs >= window['start'] and minute_abs < window['end']:
                return window_n
            window_n += 1
        return -1

    def clean_incrementing_reverse(self, data):
        """
        Cleanup an incrementing sensor data that runs backwards in time to remove the
        resets (where it goes back to 0) and make it always increment
        """
        new_data = {}
        length = len(data)

        increment = 0
        last = data[max(data)]

        for index in range(0, length):
            rindex = length - index - 1
            nxt = data[rindex]
            if nxt >= last:
                increment += nxt - last
            last = nxt
            new_data[rindex] = increment

        return new_data

    def get_from_incrementing(self, data, index):
        """
        Get a single value from an incrementing series e.g. kwh today -> kwh this minute
        """
        while index < 0:
            index += 24*60
        return data[index] - data[index + 1]

    def record_length(self, charge_window):
        """
        Limit the forecast length to either the total forecast duration or the start of the last window that falls outside the forecast
        """
        end_record = self.forecast_minutes
        max_windows = self.max_charge_windows(end_record, charge_window)
        if len(charge_window) > max_windows:
            end_record = min(end_record, charge_window[max_windows]['start'])
        return end_record - self.minutes_now
    
    def max_charge_windows(self, end_record_abs, charge_window):
        """
        Work out how many charge windows the time period covers
        """
        charge_windows = 0
        window_n = 0
        for window in charge_window:
            if end_record_abs >= window['end']:
                charge_windows = window_n + 1
            window_n += 1
        return charge_windows

    def record_status(self, message, debug=""):
        """
        Records status to HA sensor
        """
        self.set_state("predbat.status", state=message, attributes = {'friendly_name' : 'Status', 'icon' : 'mdi:information', 'debug' : debug})

    def run_prediction(self, charge_limit, charge_window, discharge_window, discharge_enable, load_minutes, pv_forecast_minute, save=None, step=5):
        """
        Run a prediction scenario given a charge limit, options to save the results or not to HA entity
        """
        six_days = 24*60*(self.days_previous - 1)
        predict_soc = {}
        predict_soc_time = {}
        minute = 0
        minute_left = self.forecast_minutes
        soc = self.soc_kw
        soc_min = self.soc_max
        charge_has_run = False
        charge_has_started = False
        discharge_has_run = False
        export_kwh = 0
        import_kwh = 0
        import_kwh_house = 0
        import_kwh_battery = 0
        load_kwh = 0
        pv_kwh = 0
        metric = self.cost_today_sofar
        metric_time = {}
        load_kwh_time = {}
        pv_kwh_time = {}
        export_kwh_time = {}
        import_kwh_time = {}

        # self.log("Sim discharge window {} enable {}".format(discharge_window, discharge_enable))

        # For the SOC calculation we need to stop 24 hours after the first charging window starts
        # to avoid wrapping into the next day
        end_record = self.record_length(charge_window)
        record = True

        # Simulate each forward minute
        while minute < self.forecast_minutes:
            minute_yesterday = 24 * 60 - minute + six_days
            # Minute yesterday can wrap if days_previous is only 1 
            minute_absolute = minute + self.minutes_now
            minute_timestamp = self.midnight_utc + timedelta(seconds=60*minute_absolute)
            charge_window_n = self.in_charge_window(charge_window, minute_absolute)
            discharge_window_n = self.in_charge_window(discharge_window, minute_absolute)

            # Outside the recording window?
            if minute >= end_record and record:
                record = False

            # Get load and pv forecast, total up for all values in the step
            pv_now = 0
            load_yesterday = 0
            for offset in range(0, step):
                pv_now += pv_forecast_minute.get(minute_absolute + offset, 0.0)
                load_yesterday += self.get_from_incrementing(load_minutes, minute_yesterday - offset)
            pv_kwh += pv_now

            # Car charging hold
            if self.car_charging_hold and self.car_charging_energy:
                # Hold based on data
                car_energy = 0
                for offset in range(0, step):
                    car_energy += self.get_from_incrementing(self.car_charging_energy, minute_yesterday - offset)

                if self.debug_enable and car_energy > 0.0 and (minute % 60) == 0 and (minute < 60*48):
                    self.log("Hour {} car charging hold with data {} load now {} metric {}".format(minute/60, car_energy, load_yesterday, metric))

                load_yesterday = max(0, load_yesterday - car_energy)
            elif self.car_charging_hold and (load_yesterday >= (self.car_charging_threshold * step)):
                # Car charging hold - ignore car charging in computation based on threshold
                load_yesterday = 0
                if self.debug_enable and (minute % 60) == 0:
                    self.log("Hour {} car charging hold".format(minute/60))

            # Octopus slot car charging?
            if self.car_charging_rate:
                car_load = self.in_octopus_slot(minute_absolute)
                if car_load > 0.0:
                    load_yesterday += car_load * step / 60.0
                    if self.debug_enable and (minute % 60) == 0:
                        self.log("Car charging now load {} at minute {}" % (load_yesterday, minute))

            # Count load
            if record:
                load_kwh += load_yesterday

            # Are we within the charging time window?
            if self.charge_enable and (charge_window_n >= 0) and soc < charge_limit[charge_window_n]:
                old_soc = soc
                soc = min(soc + (self.charge_rate * step), charge_limit[charge_window_n])

                # Apply battery loss to computed charging energy
                # For now we ignore PV in this as it's probably not a major factor when mains charging is enabled
                if record:
                    energy = max(0, soc - old_soc - pv_now) / self.battery_loss

                    # Must add in grid import for load
                    energy += load_yesterday
                    import_kwh += energy
                    import_kwh_battery += energy
                    if minute_absolute in self.rate_import:
                        metric += self.rate_import[minute_absolute] * energy
                    else:
                        metric += self.metric_battery * energy

                if self.debug_enable and (minute % 60) == 0:
                    self.log("Hour {} battery charging target soc {}".format(minute/60, charge_limit[charge_window_n]))
            elif (discharge_window_n >= 0) and (soc > self.reserve) and discharge_enable[discharge_window_n]:
                # If force discharging the battery
                # Stop when the battery runs out also
                discharge_has_run = True

                # Work out draw
                battery_draw = self.discharge_rate * step
                if soc - self.reserve < battery_draw:
                    battery_draw = soc - self.reserve
                soc -= battery_draw
                diff = load_yesterday - pv_now - battery_draw

                if diff >= 0:
                    # Importing despite full draw?
                    energy = diff
                    if record:
                        import_kwh += energy
                        import_kwh_house += energy
                        if minute_absolute in self.rate_import:
                            metric += self.rate_import[minute_absolute] * energy
                        else:
                            metric += self.metric_house * energy
                else:
                    # Export
                    energy = -diff
                    if record:
                        export_kwh += energy
                        #  self.log("Discharging minute {} rate {} diff {} export {}".format(minute, battery_draw, diff, energy))
                        if minute_absolute in self.rate_export:
                            metric -= self.rate_export[minute_absolute] * energy
                        else:
                            metric -= self.metric_export * energy

            else:
                diff = load_yesterday - pv_now

                # Apply battery loss to charging from PV
                if diff < 0:
                    diff *= self.battery_loss

                # Max charge rate, export over the cap
                if diff < -(self.charge_rate * step):
                    soc -= self.charge_rate * step
                    if record:
                        energy = -(diff + self.charge_rate * step)
                        export_kwh += energy
                        if minute_absolute in self.rate_export:
                            metric -= self.rate_export[minute_absolute] * energy
                        else:
                            metric -= self.metric_export * energy

                # Max discharge rate, draw from grid over the cap
                if diff > (self.discharge_rate * step):
                    soc -= self.discharge_rate * step
                    if record:
                        energy = diff - (self.discharge_rate * step)
                        import_kwh += energy
                        if self.charge_enable and (charge_window_n >= 0):
                            # If the battery is on charge anyhow then imports are kind of the same as battery charging (price wise)
                            import_kwh_battery += energy
                        else:
                            # self.log("importing to minute %s amount %s kw total %s kwh total draw %s" % (minute, energy, import_kwh_house, diff))
                            import_kwh_house += energy

                        if minute_absolute in self.rate_import:
                            metric += self.rate_import[minute_absolute] * energy
                        else:
                            if self.charge_enable and (charge_window_n >= 0):
                                metric += self.metric_battery * energy
                            else:
                                metric += self.metric_house * energy
                else:
                    soc -= diff

            # Flat battery, draw from grid over the cap
            if soc < self.reserve:
                if record:
                    energy = self.reserve - soc
                    import_kwh += energy
                    import_kwh_house += energy
                    if minute_absolute in self.rate_import:
                        metric += self.rate_import[minute_absolute] * energy
                    else:
                        metric += self.metric_house * energy
                soc = self.reserve

            # Full battery, export over the cap
            if soc > self.soc_max:
                if record:
                    energy = soc - self.soc_max
                    export_kwh += energy
                    if minute_absolute in self.rate_export:
                        metric -= self.rate_export[minute_absolute] * energy
                    else:
                        metric -= self.metric_export * energy
                soc = self.soc_max

            if self.debug_enable and minute % 60 == 0:
                self.log("Hour {} load_yesterday {} pv_now {} soc {}".format(minute/60, load_yesterday, pv_now, soc))

            predict_soc[minute] = self.dp3(soc)

            # Only store every 10 minutes for data-set size
            if (minute % 10) == 0:
                stamp = minute_timestamp.strftime(TIME_FORMAT)
                predict_soc_time[stamp] = self.dp3(soc)
                metric_time[stamp] = self.dp2(metric)
                load_kwh_time[stamp] = self.dp3(load_kwh)
                pv_kwh_time[stamp] = self.dp2(pv_kwh)
                import_kwh_time[stamp] = self.dp2(import_kwh)
                export_kwh_time[stamp] = self.dp2(export_kwh)

            # Store the number of minutes until the battery runs out
            if record and soc <= self.reserve:
                minute_left = max(minute, minute_left)

            # Record final soc
            if record:
                final_soc = soc

            # Have we pasted the charging time
            if self.charge_enable and (charge_window_n >= 0):
                charge_has_started = True
            if self.charge_enable and charge_has_started and (charge_window_n < 0):
                charge_has_run = True

            # Record soc min
            if record and (discharge_has_run or charge_has_run or not self.charge_enable):
                soc_min = min(soc_min, soc)

            minute += step

        hours_left = minute_left / 60.0
        charge_limit_percent = [min(int((float(charge_limit[i]) / self.soc_max * 100.0) + 0.5), 100) for i in range(0, len(charge_limit))]

        if self.debug_enable or save:
            self.log("predict {} charge limit {}% ({} kwh) final soc {} kwh metric {} p min_soc {} kwh load {} pv {}".format(
                      save, charge_limit_percent, charge_limit, self.dp2(final_soc), self.dp2(metric), self.dp2(soc_min), self.dp2(load_kwh), self.dp2(pv_kwh)))

        # Save data to HA state
        if save and save=='base' and not SIMULATE:
            self.set_state("predbat.battery_hours_left", state=self.dp2(hours_left), attributes = {'friendly_name' : 'Predicted Battery Hours left', 'state_class': 'measurement', 'unit_of_measurement': 'hours', 'icon' : 'mdi:timelapse'})
            self.set_state("predbat.soc_kw_h0", state=self.dp3(predict_soc[0]), attributes = {'friendly_name' : 'Current SOC kwh', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery'})
            self.set_state("predbat.soc_kw", state=self.dp3(final_soc), attributes = {'results' : predict_soc_time, 'friendly_name' : 'Predicted SOC kwh', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery'})
            self.set_state("predbat.soc_min_kwh", state=self.dp3(soc_min), attributes = {'friendly_name' : 'Predicted minimum SOC best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery-arrow-down-outline'})
            self.publish_charge_limit(charge_limit, charge_window, charge_limit_percent, best=False)
            self.set_state("predbat.export_energy", state=self.dp3(export_kwh), attributes = {'results' : export_kwh_time, 'friendly_name' : 'Predicted exports', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-export'})
            self.set_state("predbat.load_energy", state=self.dp3(load_kwh), attributes = {'results' : load_kwh_time, 'friendly_name' : 'Predicted load', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:home-lightning-bolt'})
            self.set_state("predbat.pv_energy", state=self.dp3(pv_kwh), attributes = {'results' : pv_kwh_time, 'friendly_name' : 'Predicted PV', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:solar-power'})
            self.set_state("predbat.import_energy", state=self.dp3(import_kwh), attributes = {'results' : import_kwh_time, 'friendly_name' : 'Predicted imports', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-import'})
            self.set_state("predbat.import_energy_battery", state=self.dp3(import_kwh_battery), attributes = {'friendly_name' : 'Predicted import to battery', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-import'})
            self.set_state("predbat.import_energy_house", state=self.dp3(import_kwh_house), attributes = {'friendly_name' : 'Predicted import to house', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-import'})
            self.log("Battery has " + str(hours_left) + " hours left - now at " + str(self.soc_kw))
            self.set_state("predbat.metric", state=self.dp2(metric), attributes = {'results' : metric_time, 'friendly_name' : 'Predicted metric (cost)', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
            self.set_state("predbat.duration", state=self.dp2(end_record/60), attributes = {'friendly_name' : 'Prediction duration', 'state_class': 'measurement', 'unit_of_measurement': 'hours', 'icon' : 'mdi:arrow-split-vertical'})

        if save and save=='best' and not SIMULATE:
            self.set_state("predbat.best_battery_hours_left", state=self.dp2(hours_left), attributes = {'friendly_name' : 'Predicted Battery Hours left best', 'state_class': 'measurement', 'unit_of_measurement': 'hours', 'icon' : 'mdi:timelapse'})
            self.set_state("predbat.soc_kw_best", state=self.dp3(final_soc), attributes = {'results' : predict_soc_time, 'friendly_name' : 'Battery SOC kwh best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery'})
            self.set_state("predbat.soc_kw_best_h1", state=self.dp3(predict_soc[60]), attributes = {'friendly_name' : 'Predicted SOC kwh best + 1h', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery'})
            self.set_state("predbat.soc_kw_best_h8", state=self.dp3(predict_soc[60*8]), attributes = {'friendly_name' : 'Predicted SOC kwh best + 8h', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery'})
            self.set_state("predbat.soc_kw_best_h12", state=self.dp3(predict_soc[60*12]), attributes = {'friendly_name' : 'Predicted SOC kwh best + 12h', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery'})
            self.set_state("predbat.best_soc_min_kwh", state=self.dp3(soc_min), attributes = {'friendly_name' : 'Predicted minimum SOC best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery-arrow-down-outline'})
            self.publish_charge_limit(charge_limit, charge_window, charge_limit_percent, best=True)
            self.publish_discharge_limit(discharge_window, discharge_enable, best=True)
            self.set_state("predbat.best_export_energy", state=self.dp3(export_kwh), attributes = {'results' : export_kwh_time, 'friendly_name' : 'Predicted exports best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-export'})
            self.set_state("predbat.best_load_energy", state=self.dp3(load_kwh), attributes = {'friendly_name' : 'Predicted load best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:home-lightning-bolt'})
            self.set_state("predbat.best_pv_energy", state=self.dp3(pv_kwh), attributes = {'results' : pv_kwh_time, 'friendly_name' : 'Predicted PV best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:solar-power'})
            self.set_state("predbat.best_import_energy", state=self.dp3(import_kwh), attributes = {'results' : import_kwh_time, 'friendly_name' : 'Predicted imports best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-import'})
            self.set_state("predbat.best_import_energy_battery", state=self.dp3(import_kwh_battery), attributes = {'friendly_name' : 'Predicted import to battery best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-import'})
            self.set_state("predbat.best_import_energy_house", state=self.dp3(import_kwh_house), attributes = {'friendly_name' : 'Predicted import to house best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-import'})
            self.set_state("predbat.best_metric", state=self.dp2(metric), attributes = {'results' : metric_time, 'friendly_name' : 'Predicted best metric (cost)', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})

        if save and save=='best10' and not SIMULATE:
            self.set_state("predbat.soc_kw_best10", state=self.dp3(final_soc), attributes = {'results' : predict_soc_time, 'friendly_name' : 'Battery SOC kwh best 10%', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery'})
            self.set_state("predbat.best10_pv_energy", state=self.dp3(pv_kwh), attributes = {'results' : pv_kwh_time, 'friendly_name' : 'Predicted PV best 10%', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:solar-power'})
            self.set_state("predbat.best10_metric", state=self.dp2(metric), attributes = {'results' : metric_time, 'friendly_name' : 'Predicted best 10% metric (cost)', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})

        return metric, charge_limit_percent, import_kwh_battery, import_kwh_house, export_kwh, soc_min, final_soc

    def time_now_str(self):
        """
        Return time now as human string
        """
        return (self.midnight + timedelta(minutes=self.minutes_now)).strftime("%H:%M:%S")

    def time_abs_str(self, minute):
        """
        Return time absolute as human string
        """
        return (self.midnight + timedelta(minutes=minute)).strftime("%m-%d %H:%M:%S")

    def rate_replicate(self, rates):
        """
        We don't get enough hours of data for Octopus, so lets assume it repeats until told others
        """
        minute = 0
        # Add 12 extra hours to make sure charging period will end
        while minute < (self.forecast_minutes + 24*60):
            if minute not in rates:
                minute_mod = minute % (24*60)
                if minute_mod in rates:
                    rates[minute] = rates[minute_mod]
                else:
                    # Missing rate within 24 hours - fill with dummy high rate
                    rates[minute] = self.metric_house
            minute += 1
        return rates

    def find_charge_window(self, rates, minute, threshold_rate, find_high):
        """
        Find the charging windows based on the low rate threshold (percent below average)
        """
        rate_low_start = -1
        rate_low_end = -1
        rate_low_average = 0
        rate_low_rate = 0
        rate_low_count = 0

        stop_at = self.forecast_minutes + 12*60
        # Scan for lower rate start and end
        while minute < stop_at:
            # Don't allow starts beyond the forecast window
            if minute >= self.forecast_minutes and rate_low_start < 0:
                break

            if minute in rates:
                rate = rates[minute]
                if (not find_high and (rate <= threshold_rate)) or (find_high and (rate >= threshold_rate)):
                    if rate_low_start >= 0 and self.dp2(rate) != self.dp2(rate_low_rate):
                        # Refuse mixed rates
                        rate_low_end = minute
                        break
                    if find_high and rate_low_start >= 0 and (minute - rate_low_start) >= 30:
                        # For export slots make them all 30 minutes so we can select some not all
                        rate_low_end = minute
                        break
                    if rate_low_start < 0:
                        rate_low_start = minute
                        rate_low_end = stop_at
                        rate_low_count = 1
                        rate_low_average = rate
                        rate_low_rate = rate
                        # self.log("Rate low start %s rate %s" % (minute, rate))
                    elif rate_low_end > minute:
                        rate_low_average += rate
                        rate_low_count += 1
                else:
                    if rate_low_start >= 0:
                        # self.log("Rate low stop (too high) %s rate %s" % (minute, rate))
                        rate_low_end = minute
                        break                    
            else:
                if rate_low_start >= 0 and rate_low_end >= minute:
                    rate_low_end = minute
                break
            minute += 1

        if rate_low_count:
            rate_low_average = self.dp2(rate_low_average / rate_low_count)
        return rate_low_start, rate_low_end, rate_low_average

    def basic_rates(self, info, rtype):
        """
        Work out the energy rates based on user supplied time periods
        works on a 24-hour period only and then gets replicated later for future days
        """
        rates = {}

        # Default to house value
        for minute in range(0, 24*60):
            rates[minute] = self.metric_house

        self.log("Adding {} rate info {}".format(rtype, info))
        midnight = datetime.strptime('00:00:00', "%H:%M:%S")
        for this_rate in info:
            start = datetime.strptime(this_rate.get('start', "00:00:00"), "%H:%M:%S")
            end = datetime.strptime(this_rate.get('end', "00:00:00"), "%H:%M:%S")
            rate = this_rate.get('rate', self.metric_house)
            start_minutes = max(self.mintes_to_time(start, midnight), 0)
            end_minutes   = min(self.mintes_to_time(end, midnight), 24*60-1)

            if end_minutes <= start_minutes:
                end_minutes += 24*60

            # self.log("Found rate {} {} to {} minutes".format(rate, start_minutes, end_minutes))
            for minute in range(start_minutes, end_minutes):
                rates[minute % (24*60)] = rate

        return rates

    def in_octopus_slot(self, minute):
        """
        Is the given minute inside an Octopus slot
        """
        if self.octopus_slots:
            for slot in self.octopus_slots:
                if 'start_minutes' in slot:
                    start_minutes = slot['start_minutes']
                else:
                    start = datetime.strptime(slot['startDtUtc'], TIME_FORMAT_OCTOPUS)
                    start_minutes = max(self.mintes_to_time(start, self.midnight_utc), 0)
                    slot['start_minutes'] = start_minutes

                if 'end_minutes' in slot:
                    end_minutes = slot['end_minutes']
                else:
                    end = datetime.strptime(slot['endDtUtc'], TIME_FORMAT_OCTOPUS)
                    end_minutes   = min(self.mintes_to_time(end, self.midnight_utc), self.forecast_minutes)
                    slot['end_minutes'] = end_minutes

                slot_minutes = end_minutes - start_minutes
                slot_hours = slot_minutes / 60.0

                # Return the load in that slot
                if minute >= start_minutes and minute < end_minutes:
                    # The load expected is stored in chargeKwh for the period or use the default set by the user if not which is hourly
                    return abs(float(slot.get('chargeKwh', self.car_charging_rate * slot_hours))) / slot_hours
        return 0

    def rate_scan_export(self, rates):
        """
        Scan the rates and work out min/max and charging windows for export
        """
        rate_low_min_window = 5
        rate_high_threshold = 1.2

        rate_min, rate_max, rate_average, rate_min_minute, rate_max_minute = self.rate_minmax(rates)
        self.log("Export rates min {} max {} average {}".format(rate_min, rate_max, rate_average))

        self.rate_export_min = rate_min
        self.rate_export_max = rate_max
        self.rate_export_min_minute = rate_min_minute
        self.rate_export_max_minute = rate_max_minute
        self.rate_export_average = rate_average

        # Find charging window
        self.high_export_rates = self.rate_scan_window(rates, rate_low_min_window, rate_average * rate_high_threshold, True)

        if self.high_export_rates:
            window_n = 0
            for window in self.high_export_rates:
                rate_high_start = window['start']
                rate_high_end = window['end']
                rate_high_average = window['average']

                self.log("High rate period {} to {} @{} !".format(self.time_abs_str(rate_high_start), self.time_abs_str(rate_high_end), rate_high_average))

                rate_high_start_date = self.midnight_utc + timedelta(minutes=rate_high_start)
                rate_high_end_date = self.midnight_utc + timedelta(minutes=rate_high_end)

                time_format_time = '%H:%M:%S'

                if window_n == 0 and not SIMULATE:
                    self.set_state("predbat.high_rate_export_start", state=rate_high_start_date.strftime(time_format_time), attributes = {'friendly_name' : 'Next high export rate start', 'state_class': 'timestamp', 'icon': 'mdi:table-clock'})
                    self.set_state("predbat.high_rate_export_end", state=rate_high_end_date.strftime(time_format_time), attributes = {'friendly_name' : 'Next high export rate end', 'state_class': 'timestamp', 'icon': 'mdi:table-clock'})
                    self.set_state("predbat.high_rate_export_cost", state=rate_high_average, attributes = {'friendly_name' : 'Next high export rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
                if window_n == 1 and not SIMULATE:
                    self.set_state("predbat.high_rate_export_start_2", state=rate_high_start_date.strftime(time_format_time), attributes = {'friendly_name' : 'Next+1 high export rate start', 'state_class': 'timestamp', 'icon': 'mdi:table-clock'})
                    self.set_state("predbat.high_rate_export_end_2", state=rate_high_end_date.strftime(time_format_time), attributes = {'friendly_name' : 'Next+1 high export rate end', 'state_class': 'timestamp', 'icon': 'mdi:table-clock'})
                    self.set_state("predbat.high_rate_export_cost_2", state=rate_high_average, attributes = {'friendly_name' : 'Next+1 high export rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
                window_n += 1

        # Clear rates that aren't available
        if not self.high_export_rates and not SIMULATE:
            self.log("No high export rate period found")
            self.set_state("predbat.high_rate_export_start", state='undefined', attributes = {'friendly_name' : 'Next high export rate start', 'device_class': 'timestamp', 'icon': 'mdi:table-clock'})
            self.set_state("predbat.high_rate_export_end", state='undefined', attributes = {'friendly_name' : 'Next high export rate end', 'device_class': 'timestamp', 'icon': 'mdi:table-clock'})
            self.set_state("predbat.high_rate_export_cost", state=rate_average, attributes = {'friendly_name' : 'Next high export rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
        if len(self.high_export_rates) < 2 and not SIMULATE:
            self.set_state("predbat.high_rate_export_start_2", state='undefined', attributes = {'friendly_name' : 'Next+1 high export rate start', 'device_class': 'timestamp', 'icon': 'mdi:table-clock'})
            self.set_state("predbat.high_rate_export_end_2", state='undefined', attributes = {'friendly_name' : 'Next+1 high export rate end', 'device_class': 'timestamp', 'icon': 'mdi:table-clock'})
            self.set_state("predbat.high_rate_export_cost_2", state=rate_average, attributes = {'friendly_name' : 'Next+1 high export rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})

        return rates

    def rate_minmax(self, rates):
        """
        Work out min and max rates
        """
        rate_min = 99999
        rate_min_minute = 0
        rate_max_minute = 0
        rate_max = 0
        rate_average = 0
        rate_n = 0

        # Scan rates and find min/max/average
        minute = 0
        while minute < self.forecast_minutes:
            if minute in rates:
                rate = rates[minute]
                if rate > rate_max:
                    rate_max = rate
                    rate_max_minute = minute
                if rate < rate_min:
                    rate_min = rate
                    rate_min_minute = minute
                rate_average += rate
                rate_n += 1
            minute += 1

        if rate_n:
            rate_average /= rate_n

        return self.dp2(rate_min), self.dp2(rate_max), self.dp2(rate_average), rate_min_minute, rate_max_minute

    def rate_scan_window(self, rates, rate_low_min_window, threshold_rate, find_high):
        """
        Scan for the next high/low rate window
        """
        minute = 0
        found_rates = []

        while len(found_rates) < MAX_CHARGE_LIMITS:
            rate_low_start, rate_low_end, rate_low_average = self.find_charge_window(rates, minute, threshold_rate, find_high)
            window = {}
            window['start'] = rate_low_start
            window['end'] = rate_low_end
            window['average'] = rate_low_average

            if rate_low_start >= 0:
                if rate_low_end >= self.minutes_now and (rate_low_end - rate_low_start) >= rate_low_min_window:
                    found_rates.append(window)
                minute = rate_low_end
            else:
                break
        return found_rates

    def rate_scan(self, rates, octopus_slots):
        """
        Scan the rates and work out min/max and charging windows
        """
        rate_low_min_window = 5
        rate_low_threshold = self.get_arg('rate_low_threshold', 0.8)
        self.low_rates = []
        
        rate_min, rate_max, rate_average, rate_min_minute, rate_max_minute = self.rate_minmax(rates)
        self.log("Import rates min {} max {} average {}".format(rate_min, rate_max, rate_average))

        self.rate_min = rate_min
        self.rate_max = rate_max
        self.rate_min_minute = rate_min_minute
        self.rate_max_minute = rate_max_minute
        self.rate_average = rate_average

        # Add in any planned octopus slots
        if octopus_slots:
            for slot in octopus_slots:
                start = datetime.strptime(slot['startDtUtc'], TIME_FORMAT_OCTOPUS)
                end = datetime.strptime(slot['endDtUtc'], TIME_FORMAT_OCTOPUS)
                start_minutes = max(self.mintes_to_time(start, self.midnight_utc), 0)
                end_minutes   = min(self.mintes_to_time(end, self.midnight_utc), self.forecast_minutes)

                self.log("Octopus Intelligent slot at {}-{} assumed price {}".format(self.time_abs_str(start_minutes), self.time_abs_str(end_minutes), rate_min))
                for minute in range(start_minutes, end_minutes):
                    rates[minute] = self.rate_min
                    if self.debug_enable and (minute % 30) == 0:
                        self.log("Set min octopus rate for time {}".format(minute))

        # Find charging window
        self.low_rates = self.rate_scan_window(rates, rate_low_min_window, rate_average * rate_low_threshold, False)

        # Output rate info
        if self.low_rates:
            window_n = 0
            for window in self.low_rates:
                rate_low_start = window['start']
                rate_low_end = window['end']
                rate_low_average = window['average']

                self.log("Low rate period {} to {} @{} !".format(self.time_abs_str(rate_low_start), self.time_abs_str(rate_low_end), rate_low_average))

                rate_low_start_date = self.midnight_utc + timedelta(minutes=rate_low_start)
                rate_low_end_date = self.midnight_utc + timedelta(minutes=rate_low_end)

                time_format_time = '%H:%M:%S'

                if window_n == 0 and not SIMULATE:
                    self.set_state("predbat.low_rate_start", state=rate_low_start_date.strftime(time_format_time), attributes = {'friendly_name' : 'Next low rate start', 'state_class': 'timestamp', 'icon': 'mdi:table-clock'})
                    self.set_state("predbat.low_rate_end", state=rate_low_end_date.strftime(time_format_time), attributes = {'friendly_name' : 'Next low rate end', 'state_class': 'timestamp', 'icon': 'mdi:table-clock'})
                    self.set_state("predbat.low_rate_cost", state=rate_low_average, attributes = {'friendly_name' : 'Next low rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
                if window_n == 1 and not SIMULATE:
                    self.set_state("predbat.low_rate_start_2", state=rate_low_start_date.strftime(time_format_time), attributes = {'friendly_name' : 'Next+1 low rate start', 'state_class': 'timestamp', 'icon': 'mdi:table-clock'})
                    self.set_state("predbat.low_rate_end_2", state=rate_low_end_date.strftime(time_format_time), attributes = {'friendly_name' : 'Next+1 low rate end', 'state_class': 'timestamp', 'icon': 'mdi:table-clock'})
                    self.set_state("predbat.low_rate_cost_2", state=rate_low_average, attributes = {'friendly_name' : 'Next+1 low rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
                window_n += 1

        # Clear rates that aren't available
        if not self.low_rates and not SIMULATE:
            self.log("No low rate period found")
            self.set_state("predbat.low_rate_start", state='undefined', attributes = {'friendly_name' : 'Next low rate start', 'device_class': 'timestamp', 'icon': 'mdi:table-clock'})
            self.set_state("predbat.low_rate_end", state='undefined', attributes = {'friendly_name' : 'Next low rate end', 'device_class': 'timestamp', 'icon': 'mdi:table-clock'})
            self.set_state("predbat.low_rate_cost", state=rate_average, attributes = {'friendly_name' : 'Next low rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
        if len(self.low_rates) < 2 and not SIMULATE:
            self.set_state("predbat.low_rate_start_2", state='undefined', attributes = {'friendly_name' : 'Next+1 low rate start', 'device_class': 'timestamp', 'icon': 'mdi:table-clock'})
            self.set_state("predbat.low_rate_end_2", state='undefined', attributes = {'friendly_name' : 'Next+1 low rate end', 'device_class': 'timestamp', 'icon': 'mdi:table-clock'})
            self.set_state("predbat.low_rate_cost_2", state=rate_average, attributes = {'friendly_name' : 'Next+1 low rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})

        return rates

    def publish_rates(self, rates, export):
        """
        Publish the rates for charts
        Create rates/time every 30 minutes
        """
        rates_time = {}
        for minute in range(0, self.forecast_minutes+24*60, 30):
            minute_timestamp = self.midnight_utc + timedelta(minutes=minute)
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            rates_time[stamp] = rates[minute]

        if not SIMULATE:
            if export:
                self.set_state("predbat.rates_export", state=rates[self.minutes_now], attributes = {'results' : rates_time, 'friendly_name' : 'Export rates', 'state_class' : 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
            else:
                self.set_state("predbat.rates", state=rates[self.minutes_now], attributes = {'results' : rates_time, 'friendly_name' : 'Import rates', 'state_class' : 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
        return rates

    def today_cost(self, import_today, export_today):
        """
        Work out energy costs today (approx)
        """
        day_cost = 0
        day_energy = 0
        day_energy_export = 0
        day_cost_time = {}

        for minute in range(0, self.minutes_now):
            minute_back = self.minutes_now - minute - 1
            energy = 0
            energy = self.get_from_incrementing(import_today, minute_back)
            if export_today:
                energy_export = self.get_from_incrementing(export_today, minute_back)
            else:
                energy_export = 0
            day_energy += energy
            day_energy_export += energy_export
            day_cost += self.rate_import[minute] * energy
            day_cost -= self.rate_export[minute] * energy_export

            if (minute % 10) == 0:
                minute_timestamp = self.midnight_utc + timedelta(minutes=minute)
                stamp = minute_timestamp.strftime(TIME_FORMAT)
                day_cost_time[stamp] = self.dp2(day_cost)

        if not SIMULATE:
            self.set_state("predbat.cost_today", state=self.dp2(day_cost), attributes = {'results' : day_cost_time, 'friendly_name' : 'Cost so far today', 'state_class' : 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
        self.log("Todays energy import {} kwh export {} kwh cost {} p".format(self.dp2(day_energy), self.dp2(day_energy_export), self.dp2(day_cost)))
        return day_cost

    def publish_discharge_limit(self, discharge_window, discharge_enable, best):
        """
        Create entity to chart discharge limit
        """
        discharge_limit_time = {}
        discharge_limit_time_kw = {}
        discharge_limit = self.soc_max
        discharge_limit_percent = 100
        for minute in range(0, self.forecast_minutes + self.minutes_now, 30):
            window_n = self.in_charge_window(discharge_window, minute)
            minute_timestamp = self.midnight_utc + timedelta(minutes=minute)
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            if window_n >=0 and discharge_enable[window_n]:
                discharge_limit_time[stamp] = 0
                discharge_limit_time_kw[stamp] = 0
                discharge_limit = 0
                discharge_limit_percent = 0
            else:
                discharge_limit_time[stamp] = 100
                discharge_limit_time_kw[stamp] = self.soc_max

        if not SIMULATE:
            if best:
                self.set_state("predbat.best_discharge_limit_kw", state=self.dp2(discharge_limit), attributes = {'results' : discharge_limit_time_kw, 'friendly_name' : 'Predicted discharge limit kwh best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' :'mdi:battery-charging'})
                self.set_state("predbat.best_discharge_limit", state=discharge_limit_percent, attributes = {'results' : discharge_limit_time, 'friendly_name' : 'Predicted discharge limit best', 'state_class': 'measurement', 'unit_of_measurement': '%', 'icon' :'mdi:battery-charging'})
            else:
                self.set_state("predbat.discharge_limit_kw", state=self.dp2(discharge_limit), attributes = {'results' : discharge_limit_time_kw, 'friendly_name' : 'Predicted discharge limit kwh', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' :'mdi:battery-charging'})
                self.set_state("predbat.discharge_limit", state=discharge_limit_percent, attributes = {'results' : discharge_limit_time, 'friendly_name' : 'Predicted discharge limit', 'state_class': 'measurement', 'unit_of_measurement': '%', 'icon' :'mdi:battery-charging'})

    def publish_charge_limit(self, charge_limit, charge_window, charge_limit_percent, best):
        """
        Create entity to chart charge limit
        """
        charge_limit_time = {}
        charge_limit_time_kw = {}
        for minute in range(0, self.forecast_minutes + self.minutes_now, 30):
            window = self.in_charge_window(charge_window, minute)
            minute_timestamp = self.midnight_utc + timedelta(minutes=minute)
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            if window >= 0:
                charge_limit_time[stamp] = charge_limit_percent[window]
                charge_limit_time_kw[stamp] = charge_limit[window]
            else:
                charge_limit_time[stamp] = 0
                charge_limit_time_kw[stamp] = 0
        
        if not SIMULATE:
            charge_limit_first = self.reserve
            charge_limit_percent_first = 0
            if charge_limit:
                charge_limit_first = charge_limit[0]
                charge_limit_percent_first = charge_limit_percent[0]
            if best:
                self.set_state("predbat.best_charge_limit_kw", state=self.dp2(charge_limit_first), attributes = {'results' : charge_limit_time_kw, 'friendly_name' : 'Predicted charge limit kwh best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' :'mdi:battery-charging'})
                self.set_state("predbat.best_charge_limit", state=charge_limit_percent_first, attributes = {'results' : charge_limit_time, 'friendly_name' : 'Predicted charge limit best', 'state_class': 'measurement', 'unit_of_measurement': '%', 'icon' :'mdi:battery-charging'})
            else:
                self.set_state("predbat.charge_limit_kw", state=self.dp2(charge_limit_first), attributes = {'results' : charge_limit_time_kw, 'friendly_name' : 'Predicted charge limit kwh', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' :'mdi:battery-charging'})
                self.set_state("predbat.charge_limit", state=charge_limit_percent_first, attributes = {'results' : charge_limit_time, 'friendly_name' : 'Predicted charge limit', 'state_class': 'measurement', 'unit_of_measurement': '%', 'icon' :'mdi:battery-charging'})

    def reset(self):
        """
        Init stub
        """
        self.midnight = None
        self.midnight_utc = None
        self.difference_minutes = 0
        self.minutes_now = 0
        self.minutes_to_midnight = 0
        self.days_previous = 0
        self.forecast_days = 0
        self.forecast_minutes = 0
        self.soc_kw = 0
        self.soc_max = 0
        self.metric_house = 0
        self.metric_battery = 0
        self.metric_export = 0
        self.metric_min_improvement = 0
        self.rate_import = {}
        self.rate_export = {}
        self.rate_slots = []
        self.low_rates = []
        self.cost_today_sofar = 0
        self.octopus_slots = []
        self.reserve = 0
        self.battery_loss = 1.0
        self.battery_scaling = 1.0
        self.best_soc_min = 0
        self.best_soc_margin = 0
        self.best_soc_keep = 0
        self.rate_min = 0
        self.rate_min_minute = 0
        self.rate_max = 0
        self.rate_max_minute = 0
        self.rate_average = 0
        self.rate_export_min = 0
        self.rate_export_min_minute = 0
        self.rate_export_max = 0
        self.rate_export_max_minute = 0
        self.rate_export_average = 0
        self.set_soc_minutes = 0
        self.set_window_minutes = 0
        self.debug_enable = False
        self.import_today = {}
        self.export_today = {}
        self.current_charge_limit = 0
        self.charge_enable = False
        self.charge_window = []
        self.charge_limit = []
        self.charge_window_best = []
        self.charge_limit_best = []
        self.discharge_window = []
        self.discharge_enable = []
        self.discharge_enable_best = []
        self.discharge_window_best = []
        self.charge_rate = 0
        self.discharge_rate = 0
        self.car_charging_hold = False
        self.car_charging_threshold = 99
        self.car_charging_energy = {}
        self.simulate_offset = 0
        self.sim_soc = 100
        self.sim_inverter_mode = "Eco"
        self.sim_charge_start_time = "00:00:00"
        self.sim_charge_end_time = "00:00:00"
        self.sim_discharge_start = "00:00"
        self.sim_discharge_end = "23:59"
        self.sim_charge_schedule_enable = True

    def optimise_charge_limit(self, window_n, record_charge_windows, try_charge_limit, charge_window, discharge_window, discharge_enable, load_minutes, pv_forecast_minute, pv_forecast_minute10):
        """
        Optimise a single charging window for best SOC
        """
        loop_soc = self.soc_max
        best_soc = self.soc_max
        best_soc_min = self.soc_max
        best_metric = 9999999
        best_cost = 0
        prev_soc = self.soc_max + 1
        prev_metric = 9999999
        
        while loop_soc >= 0:
            was_debug = self.debug_enable
            self.debug_enable = False

            # Apply user clamping to the value we try
            try_soc = max(self.best_soc_min, loop_soc)
            try_soc = max(try_soc, self.reserve)
            try_soc = self.dp2(min(try_soc, self.soc_max))

            # Stop when we won't change the soc anymore
            if try_soc >= prev_soc:
                break

            # Store try value into the dinwo
            try_charge_limit[window_n] = try_soc

            # Simulate with medium PV
            metricmid, charge_limit_percent, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc = self.run_prediction(try_charge_limit, charge_window, discharge_window, discharge_enable, load_minutes, pv_forecast_minute)

            # Simulate with 10% PV 
            metric10, charge_limit_percent10, import_kwh_battery10, import_kwh_house10, export_kwh10, soc_min10, soc10 = self.run_prediction(try_charge_limit, charge_window, discharge_window, discharge_enable, load_minutes, pv_forecast_minute10)

            # Store simulated mid value
            metric = metricmid
            cost = metricmid

            # Balancing payment to account for battery left over 
            # ie. how much extra battery is worth to us in future, assume it's the same as low rate
            metric -= soc * self.rate_min

            # Metric adjustment based on 10% outcome weighting
            if metric10 > metricmid:
                metric_diff = metric10 - metricmid
                metric_diff *= self.get_arg('pv_metric10_weight', 0.0)
                metric += metric_diff
                metric = self.dp2(metric)

            self.debug_enable = was_debug
            if self.debug_enable:
                self.log("Sim: SOC {} window {} imp bat {} house {} exp {} min_soc {} soc {} cost {} metric {} metricmid {} metric10 {}".format
                        (try_soc, window_n, self.dp2(import_kwh_battery), self.dp2(import_kwh_house), self.dp2(export_kwh), self.dp2(soc_min), self.dp2(soc), self.dp2(cost), self.dp2(metric), self.dp2(metricmid), self.dp2(metric10)))

            # Only select the lower SOC if it makes a notable improvement has defined by min_improvement (divided in M windows)
            # and it doesn't fall below the soc_keep threshold 
            if (metric + (self.metric_min_improvement / record_charge_windows)) <= best_metric and (best_metric==9999999 or soc_min >= self.best_soc_keep):
                best_metric = metric
                best_soc = try_soc
                best_cost = cost
                best_soc_min = soc_min
                if self.debug_enable:
                    self.log("Selecting metric {} cost {} soc {} - soc_min {} and keep {}".format(metric, cost, try_soc, soc_min, self.best_soc_keep))
            else:
                if self.debug_enable:
                    self.log("Not Selecting metric {} cost {} soc {} - soc_min {} and keep {}".format(metric, cost, try_soc, soc_min, self.best_soc_keep))
            
            prev_soc = try_soc
            prev_metric = metric
            loop_soc -= max(self.get_arg('best_soc_step', 0.5), 0.1)

        # Add margin last
        best_soc = min(best_soc + self.best_soc_margin, self.soc_max)

        return best_soc, best_metric, best_cost, best_soc_min

    def optimise_discharge(self, window_n, record_charge_windows, try_charge_limit, charge_window, discharge_window, try_discharge, load_minutes, pv_forecast_minute, pv_forecast_minute10):
        """
        Optimise a single charging window for best SOC
        """
        best_discharge = False
        best_metric = 9999999
        best_cost = 0
        
        for this_discharge_enable in [False, True]:
            was_debug = self.debug_enable
            self.debug_enable = False

            # Store try value into the window
            try_discharge[window_n] = this_discharge_enable

            # Simulate with medium PV
            metricmid, charge_limit_percent, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc = self.run_prediction(try_charge_limit, charge_window, discharge_window, try_discharge, load_minutes, pv_forecast_minute)

            # Simulate with 10% PV 
            metric10, charge_limit_percent10, import_kwh_battery10, import_kwh_house10, export_kwh10, soc_min10, soc10 = self.run_prediction(try_charge_limit, charge_window, discharge_window, try_discharge, load_minutes, pv_forecast_minute10)

            # Store simulated mid value
            metric = metricmid
            cost = metricmid

            # Balancing payment to account for battery left over 
            # ie. how much extra battery is worth to us in future, assume it's the same as low rate
            metric -= soc * self.rate_min

            # Metric adjustment based on 10% outcome weighting
            if metric10 > metricmid:
                metric_diff = metric10 - metricmid
                metric_diff *= self.get_arg('pv_metric10_weight', 0.0)
                metric += metric_diff
                metric = self.dp2(metric)

            self.debug_enable = was_debug
            if self.debug_enable:
                self.log("Sim: Discharge {} window {} imp bat {} house {} exp {} min_soc {} soc {} cost {} metric {} metricmid {} metric10 {}".format
                        (this_discharge_enable, window_n, self.dp2(import_kwh_battery), self.dp2(import_kwh_house), self.dp2(export_kwh), self.dp2(soc_min), self.dp2(soc), self.dp2(cost), self.dp2(metric), self.dp2(metricmid), self.dp2(metric10)))

            # Only select the lower SOC if it makes a notable improvement has defined by min_improvement (divided in M windows)
            # and it doesn't fall below the soc_keep threshold 
            if (metric + (self.metric_min_improvement / record_charge_windows)) <= best_metric and (soc_min >= self.best_soc_keep):
                best_metric = metric
                best_discharge = this_discharge_enable
                best_cost = cost
                if self.debug_enable:
                    self.log("Selecting metric {} cost {} discharge {} - soc_min {} and keep {}".format(metric, cost, this_discharge_enable, soc_min, self.best_soc_keep))
            else:
                if self.debug_enable:
                    self.log("Not Selecting metric {} cost {} discharge {} - soc_min {} and keep {}".format(metric, cost, this_discharge_enable, soc_min, self.best_soc_keep))
            
 
        return best_discharge, best_metric, best_cost, soc_min

    def window_sort_func(self, window):
        """
        Helper sort index function
        """
        return window['average']

    def sort_window_by_price(self, windows):
        """
        Sort the charge windows by lowest price first, return a list of window IDs
        """
        window_with_id = windows[:]
        wid = 0
        for window in window_with_id:
            window['id'] = wid
            wid += 1
        window_with_id.sort(key=self.window_sort_func)
        id_list = []
        for window in window_with_id:
            id_list.append(window['id'])
        # self.log("Sorted window ids {}".format(id_list))
        return id_list

    def discard_unused_charge_slots(self, charge_limit_best, charge_window_best, reserve):
        """
        Filter out unused charge slots (those set at reserve)
        """
        new_limit_best = []
        new_window_best = []

        max_slots = len(charge_limit_best)

        for window_n in range(0, max_slots):
            # Only keep slots > than reserve, or keep the last one so we don't have zero slots
            if charge_limit_best[window_n] > self.dp2(reserve):
                new_limit_best.append(charge_limit_best[window_n])
                new_window_best.append(charge_window_best[window_n])
        return new_limit_best, new_window_best 

    def discard_unused_discharge_slots(self, discharge_enable_best, discharge_window_best):
        """
        Filter out the windows we disabled
        """
        new_best = []
        new_enable = []
        for window_n in range(0, len(discharge_enable_best)):
            if discharge_enable_best[window_n]:
                # Also merge contigous enabled windows
                if new_best and discharge_window_best[window_n]['start'] == new_best[-1]['end']:
                    new_best[-1]['end'] = discharge_window_best[window_n]['end']
                else:
                    new_best.append(discharge_window_best[window_n])
                    new_enable.append(True)

        return new_enable, new_best

    def update_pred(self):
        """
        Update the prediction state, everything is called from here right now
        """
        local_tz = pytz.timezone(self.get_arg('timezone', "Europe/London"))
        now_utc = datetime.now(local_tz)
        now = datetime.now()
        if SIMULATE:
            now += timedelta(minutes=self.simulate_offset)
            now_utc += timedelta(minutes=self.simulate_offset)

        self.log("PredBat - update at: " + str(now_utc))

        self.debug_enable = self.get_arg('debug_enable', False)
        self.log("Debug enable is {}".format(self.debug_enable))

        self.midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

        self.difference_minutes = self.minutes_since_yesterday(now)
        self.minutes_now = int((now - self.midnight).seconds / 60)
        self.minutes_to_midnight = 24*60 - self.minutes_now

        self.days_previous = self.get_arg('days_previous', 7)
        forecast_hours = self.get_arg('forecast_hours', 24)
        self.forecast_days = int((forecast_hours + 23)/24)
        self.forecast_minutes = forecast_hours * 60

        load_minutes = self.minute_data_load(now_utc)

        self.metric_house = self.get_arg('metric_house', 38.0)
        self.metric_battery = self.get_arg('metric_battery', 7.5)
        self.metric_export = self.get_arg('metric_export', 4)
        self.metric_min_improvement = self.get_arg('metric_min_improvement', 5)
        self.rate_import = {}
        self.rate_export = {}
        self.rate_slots = []
        self.low_rates = []
        self.octopus_slots = []
        self.cost_today_sofar = 0

        # Basic rates defined by user over time
        if 'rates_import' in self.args:
            self.rate_import = self.basic_rates(self.get_arg('rates_import', indirect=False), 'import')
        if 'rates_export' in self.args:
            self.rate_export = self.basic_rates(self.get_arg('rates_export', indirect=False), 'export')

        # Octopus import rates
        if 'metric_octopus_import' in self.args:
            data_import = self.get_state(entity_id = self.get_arg('metric_octopus_import', indirect=False), attribute='rates')
            if data_import:
                self.rate_import = self.minute_data(data_import, self.forecast_days, self.midnight_utc, 'rate', 'from', backwards=False, to_key='to')
            else:
                self.log("Warning: metric_octopus_import is not set correctly, ignoring..")
        
        # Octopus intelligent slots
        if 'octopus_intelligent_slot' in self.args:
            completed = self.get_state(entity_id = self.get_arg('octopus_intelligent_slot', indirect=False), attribute='completedDispatches')
            if completed:
                self.octopus_slots += completed
            planned = self.get_state(entity_id = self.get_arg('octopus_intelligent_slot', indirect=False), attribute='plannedDispatches')
            if planned:
                self.octopus_slots += planned

        # Fixed URL for rate import
        if 'rates_import_octopus_url' in self.args:
            self.log("Downloading import rates directly from url {}".format(self.get_arg('rates_import_octopus_url', indirect=False)))
            self.rate_import = self.download_octopus_rates(self.get_arg('rates_import_octopus_url', indirect=False))

        # Replicate and scan rates
        if self.rate_import:
            self.rate_import = self.rate_replicate(self.rate_import)
            self.rate_import = self.rate_scan(self.rate_import, self.octopus_slots)
            self.publish_rates(self.rate_import, False)
        else:
            self.log("No import rate data provided - using default metric")

        # Octopus export rates
        if 'metric_octopus_export' in self.args:
            data_export = self.get_state(entity_id = self.get_arg('metric_octopus_export', indirect=False), attribute='rates')
            if data_export:
                self.rate_export = self.minute_data(data_export, self.forecast_days, self.midnight_utc, 'rate', 'from', backwards=False, to_key='to')
            else:
                self.log("Warning: metric_octopus_export is not set correctly, ignoring..")

        # Fixed URL for rate export
        if 'rates_export_octopus_url' in self.args:
            self.log("Downloading export rates directly from url {}".format(self.get_arg('rates_export_octopus_url', indirect=False)))
            self.rate_export = self.download_octopus_rates(self.get_arg('rates_export_octopus_url', indirect=False))

        # Replicate rates for export
        if self.rate_export:
            self.rate_export = self.rate_scan_export(self.rate_export)
            self.rate_export = self.rate_replicate(self.rate_export)
            self.publish_rates(self.rate_export, True)
        else:
            self.log("No export rate data provided - using default metric")

        # Load import today data 
        if 'import_today' in self.args and self.rate_import:
            self.import_today = self.minute_data_import_export(now_utc, 'import_today')
        else:
            self.import_today = {}

        # Load export today data 
        if 'export_today' in self.args and self.rate_export:
            self.export_today = self.minute_data_import_export(now_utc, 'export_today')
        else:
            self.export_today = {}

        # Work out cost today
        if self.import_today:
            self.cost_today_sofar = self.today_cost(self.import_today, self.export_today)

        # Battery charging options
        self.battery_loss = 1.0 - self.get_arg('battery_loss', 0.05)
        self.battery_scaling = self.get_arg('battery_scaling', 1.0)
        self.best_soc_margin = self.get_arg('best_soc_margin', 0)
        self.best_soc_min = self.get_arg('best_soc_min', 0.5)
        self.best_soc_keep = self.get_arg('best_soc_keep', 0.5)
        self.set_soc_minutes = self.get_arg('set_soc_minutes', 30)
        self.set_window_minutes = self.get_arg('set_window_minutes', 30)
        self.charge_rate = float(self.get_arg('charge_rate', combine=True, attribute='max')) / 1000.0 / 60.0
        self.discharge_rate = float(self.get_arg('discharge_rate', combine=True, attribute='max')) / 1000.0 / 60.0

        # Find the inverters
        self.num_inverters = int(self.get_arg('num_inverters', 1))
        self.inverters = []
        self.charge_enable = False
        self.charge_window = []
        self.discharge_window = []
        self.discharge_enable = []
        self.soc_kw = 0
        self.soc_max = 0
        self.reserve = 0

        for id in range(0, self.num_inverters):
            inverter = Inverter(self, id)
            inverter.update_status(self.minutes_now)
            # As the inverters will run in lockstep, we will initially look at the programming of the first enabled one for the current window setting
            if inverter.charge_enable and not self.charge_enable:
                self.charge_enable = True
                self.current_charge_limit = inverter.current_charge_limit
                self.charge_window = inverter.charge_window
                self.discharge_window = inverter.discharge_window
                self.discharge_enable = inverter.discharge_enable
            self.soc_max += inverter.soc_max
            self.soc_kw += inverter.soc_kw
            self.reserve += inverter.reserve
            self.inverters.append(inverter)
        self.log("Found {} inverters total reserve {} soc_max {} soc {} ".format(len(self.inverters), self.reserve, self.soc_max, self.soc_kw))

        # Work out current charge limits
        self.charge_limit = [self.current_charge_limit * self.soc_max / 100.0 for i in range(0, len(self.charge_window))]
        self.charge_limit_percent = [self.current_charge_limit for i in range(0, len(self.charge_window))]
        self.log("Base charge limit {} percent {}".format(self.charge_limit, self.charge_limit_percent))

        # Calculate best charge windows
        if self.low_rates:
            # If we are using calculated windows directly then save them
            self.charge_window_best = self.low_rates[:]
            self.log('Charge windows best will be {}'.format(self.charge_window_best))
        else:
            # Default best charge window as this one
            self.charge_window_best = self.charge_window

        # Calculate best discharge windows
        if self.high_export_rates:
            self.discharge_window_best = self.high_export_rates[:]
            self.log('Discharge windows best will be {}'.format(self.discharge_window_best))
        else:
            self.discharge_window_best = []

        # Pre-fill best charge limit with the current charge limit
        self.charge_limit_best = [self.current_charge_limit * self.soc_max / 100.0 for i in range(0, len(self.charge_window_best))]
        self.charge_limit_percent_best = [self.current_charge_limit for i in range(0, len(self.charge_window_best))]

        # Pre-fill best discharge enable with Off
        self.discharge_enable_best = [False for i in range(0, len(self.discharge_window_best))]

        # Fetch PV forecast if enbled, today must be enabled, other days are optional
        if 'pv_forecast_today' in self.args:
            pv_forecast_data    = self.get_state(entity_id = self.get_arg('pv_forecast_today', indirect=False), attribute='detailedForecast')
            if 'pv_forecast_tomorrow' in self.args:
                pv_forecast_data += self.get_state(entity_id = self.get_arg('pv_forecast_tomorrow', indirect=False), attribute='detailedForecast')
            if 'pv_forecast_d3' in self.args:
                pv_forecast_data += self.get_state(entity_id = self.get_arg('pv_forecast_d3', indirect=False), attribute='detailedForecast')
            if 'pv_forecast_d4' in self.args:
                pv_forecast_data += self.get_state(entity_id = self.get_arg('pv_forecast_d4', indirect=False), attribute='detailedForecast')
            if 'pv_forecast_d5' in self.args:
                pv_forecast_data += self.get_state(entity_id = self.get_arg('pv_forecast_d5', indirect=False), attribute='detailedForecast')
            if 'pv_forecast_d6' in self.args:
                pv_forecast_data += self.get_state(entity_id = self.get_arg('pv_forecast_d6', indirect=False), attribute='detailedForecast')
            if 'pv_forecast_d7' in self.args:
                pv_forecast_data += self.get_state(entity_id = self.get_arg('pv_forecast_d7', indirect=False), attribute='detailedForecast')
            pv_forecast_minute = self.minute_data(pv_forecast_data, self.forecast_days, self.midnight_utc, 'pv_estimate' + str(self.get_arg('pv_estimate', '')), 'period_start', backwards=False, divide_by=30, scale=self.get_arg('pv_scaling', 1.0))
            pv_forecast_minute10 = self.minute_data(pv_forecast_data, self.forecast_days, self.midnight_utc, 'pv_estimate10', 'period_start', backwards=False, divide_by=30, scale=self.get_arg('pv_scaling', 1.0))
        else:
            pv_forecast_minute = {}
            pv_forecast_minute10 = {}

        # Car charging hold - when enabled battery is held during car charging in simulation
        self.car_charging_hold = self.get_arg('car_charging_hold', False)
        self.car_charging_threshold = float(self.get_arg('car_charging_threshold', 6.0)) / 60.0
        self.car_charging_rate = self.get_arg('octopus_intelligent_charge_rate', 0.0)

        self.car_charging_energy = {}
        if 'car_charging_energy' in self.args:
            self.car_charging_energy = self.minute_data(self.get_history(entity_id = self.get_arg('car_charging_energy', indirect=False), days = self.days_previous + 1)[0], 
                                                        self.days_previous + 1, now_utc, 'state', 'last_updated', backwards=True, smoothing=True, clean_increment=True)
            self.log("Car charging hold {} with energy data".format(self.car_charging_hold))
        else:
            self.log("Car charging hold {} threshold {}".format(self.car_charging_hold, self.car_charging_threshold*60.0))

        # Simulate current settings (inverter 0)
        metric, self.charge_limit_percent, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc = self.run_prediction(self.charge_limit, self.charge_window, self.discharge_window, self.discharge_enable, load_minutes, pv_forecast_minute, save='base')

        # Try different battery SOCs to get the best result
        if self.get_arg('calculate_best', False):
            end_record = self.record_length(self.charge_window_best)
            record_charge_windows = max(self.max_charge_windows(end_record + self.minutes_now, self.charge_window_best), 1)
            self.log("Record charge windows is {} end_record_abs was {}".format(record_charge_windows, end_record + self.minutes_now))
            
            # Set all to min
            self.charge_limit_best = [self.reserve if n < record_charge_windows else self.soc_max for n in range(0, len(self.charge_limit_best))]

            # Optimise in price order, cheapest first
            for window_n in self.sort_window_by_price(self.charge_window_best[:record_charge_windows]):
                best_soc, best_metric, best_cost, soc_min = self.optimise_charge_limit(window_n, record_charge_windows, self.charge_limit_best, self.charge_window_best, self.discharge_window_best, self.discharge_enable_best, load_minutes, pv_forecast_minute, pv_forecast_minute10)

                if self.debug_enable or 1:
                    self.log("Best charge limit window {} (adjusted) soc calculated at {} min {} (margin added {} and min {}) with metric {} cost {} windows {}".format(window_n, self.dp2(best_soc), self.dp2(soc_min), self.best_soc_margin, self.best_soc_min, self.dp2(best_metric), self.dp2(best_cost), self.charge_limit_best))
                self.charge_limit_best[window_n] = best_soc

        # Discard unused slots if we controlling the charge window
        if self.get_arg('set_charge_window', False):
            self.charge_limit_best, self.charge_window_best = self.discard_unused_charge_slots(self.charge_limit_best, self.charge_window_best, self.reserve)
            self.log("Filtered charge windows {} {} reserve {}".format(self.charge_limit_best, self.charge_window_best, self.reserve))
        else:
            self.log("Unfiltered charge windows {} {} reserve {}".format(self.charge_limit_best, self.charge_window_best, self.reserve))

        # Try different discharge options
        if self.get_arg('set_discharge_window', False) and self.discharge_window_best:
            end_record = self.record_length(self.discharge_window_best)
            record_discharge_windows = max(self.max_charge_windows(end_record + self.minutes_now, self.discharge_window_best), 1)

            # Set all to off
            self.discharge_enable_best = [False for n in range(0, len(self.discharge_window_best))]

            for window_n in range(0, record_discharge_windows):
                best_discharge, best_metric, best_cost, soc_min = self.optimise_discharge(window_n, record_discharge_windows, self.charge_limit_best, self.charge_window_best, self.discharge_window_best, self.discharge_enable_best, load_minutes, pv_forecast_minute, pv_forecast_minute10)
                if self.debug_enable or 1:
                    self.log("Best discharge limit window {} discharge {} (adjusted) soc calculated at {} min {} (margin added {} and min {}) with metric {} cost {}".format(window_n, best_discharge, self.dp2(best_soc), self.dp2(soc_min), self.best_soc_margin, self.best_soc_min, self.dp2(best_metric), self.dp2(best_cost)))
                self.discharge_enable_best[window_n] = best_discharge
            
            # Filter out the windows we disabled
            self.discharge_enable_best, self.discharge_window_best = self.discard_unused_discharge_slots(self.discharge_enable_best, self.discharge_window_best)
            self.log("Discharge windows now {} {}".format(self.discharge_enable_best, self.discharge_window_best))

        # Final simulation of best, do 10% and normal scenario
        best_metric10, self.charge_limit_percent_best10, import_kwh_battery10, import_kwh_house10, export_kwh10, soc_min10, soc10 = self.run_prediction(self.charge_limit_best, self.charge_window_best, self.discharge_window_best, self.discharge_enable_best, load_minutes, pv_forecast_minute10, save='best10')
        best_metric, self.charge_limit_percent_best, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc = self.run_prediction(self.charge_limit_best, self.charge_window_best, self.discharge_window_best, self.discharge_enable_best, load_minutes, pv_forecast_minute, save='best')
        self.log("Best charging limit socs {} export {} gives import battery {} house {} export {} metric {} metric10 {}".format
        (self.charge_limit_best, self.discharge_enable_best, self.dp2(import_kwh_battery), self.dp2(import_kwh_house), self.dp2(export_kwh), self.dp2(best_metric), self.dp2(best_metric10)))

        status = "Idle"
        for inverter in self.inverters:
            if inverter.charge_enable:
                # Re-programme charge window based on low rates?
                if self.get_arg('set_charge_window', False) and self.charge_window_best:
                    # Find the next best window and save it
                    window = self.charge_window_best[0]
                    minutes_start = window['start']
                    minutes_end = window['end']

                    # Combine contigous windows
                    for windows in self.charge_window_best:
                        if minutes_end == windows['start']:
                            minutes_end = windows['end']
                            self.log("Combine window with next window {}-{}".format(self.time_abs_str(windows['start']), self.time_abs_str(windows['end'])))

                    # Avoid adjust avoid start time forward when it's already started
                    if (inverter.charge_start_time_minutes < self.minutes_now) and (self.minutes_now >= minutes_start):
                        self.log("Include original start {} with our start which is {}".format(inverter.charge_start_time_minutes, minutes_start))
                        minutes_start = inverter.charge_start_time_minutes

                    # Check if end is within 24 hours of now and end is in the future
                    if (minutes_end - self.minutes_now) < 24*60 and minutes_end > self.minutes_now:
                        charge_start_time = self.midnight_utc + timedelta(minutes=minutes_start)
                        charge_end_time = self.midnight_utc + timedelta(minutes=minutes_end)
                        self.log("Charge window will be: {} - {}".format(charge_start_time, charge_end_time))

                        # Status flag
                        if self.minutes_now >= minutes_start and self.minutes_now < minutes_end:
                            status = "Charging"

                        # We must re-program if we are about to start a new charge window
                        # or the currently configured window is about to start but hasn't yet started (don't change once it's started)
                        if (self.minutes_now < minutes_end) and (
                            (minutes_start - self.minutes_now) <= self.set_window_minutes or 
                            (inverter.charge_start_time_minutes - self.minutes_now) <= self.set_window_minutes
                            ):
                            self.log("Configuring charge window now (now {} target set_window_minutes {} charge start time {}".format(self.time_abs_str(self.minutes_now), self.set_window_minutes, self.time_abs_str(minutes_start)))
                            inverter.adjust_charge_window(charge_start_time, charge_end_time)                        
                        else:
                            self.log("Not setting charging window yet as not within the window (now {} target set_window_minutes {} charge start time {}".format(self.time_abs_str(self.minutes_now),self.set_window_minutes, self.time_abs_str(minutes_start)))

                        # Set configured window minutes for the SOC adjustment routine
                        inverter.charge_start_time_minutes = minutes_start
                        inverter.charge_end_time_minutes = minutes_end
                    elif (minutes_end >= 24*60):
                        # No charging require in the next 24 hours
                        self.log("No charge windows required for 24 hours")
                        inverter.disable_charge_window()
                elif self.get_arg('set_charge_window', False):
                    # No charge windows
                    self.log("No charge windows found")
                    inverter.disable_charge_window()

                # Set forced discharge window
                if self.get_arg('set_discharge_window', False) and self.discharge_window_best:
                    window = self.discharge_window_best[0]
                    minutes_start = window['start']
                    minutes_end = window['end']
                    discharge_start_time = self.midnight_utc + timedelta(minutes=minutes_start)
                    discharge_end_time = self.midnight_utc + timedelta(minutes=minutes_end)
                    self.log("Next discharge window will be: {} - {}".format(discharge_start_time, discharge_end_time))
                    if (self.minutes_now >= minutes_start) and (self.minutes_now < minutes_end) and self.discharge_enable_best[0]:
                        self.adjust_force_discharge(True, discharge_start_time, discharge_end_time)
                        status = "Discharging"
                    else:
                        if (self.minutes_now < minutes_end) and ((minutes_start - self.minutes_now) <= self.set_window_minutes) and self.discharge_enable_best[0]:
                            inverter.adjust_force_discharge(False, discharge_start_time, discharge_end_time)
                        else:
                            self.log("Not setting discharge time as we are not yet within the window - next time is {} - {}".format(self.time_abs_str(minutes_start), self.time_abs_str(minutes_end)))
                            inverter.adjust_force_discharge(False)
                
                # Set the SOC just before or within the charge window
                if self.get_arg('set_soc_enable', False):
                    if (self.minutes_now < inverter.charge_end_time_minutes) and (inverter.charge_start_time_minutes - self.minutes_now) <= self.set_soc_minutes:
                        inverter.adjust_battery_target(self.charge_limit_percent_best[0])
                    else:
                        self.log("Not setting charging SOC as we are not within the window (now {} target set_soc_minutes {} charge start time {}".format(self.time_abs_str(self.minutes_now), self.set_soc_minutes, self.time_abs_str(inverter.charge_start_time_minutes)))


        self.log("Completed run status {}".format(status))
        self.record_status(status, debug="best_soc={} window={} discharge={}".format(self.charge_limit_best, self.charge_window_best,self.discharge_window_best))

    def auto_config(self):
        """
        Auto configure
        match arguments with sensors
        """

        states = self.get_state()
        state_keys = states.keys()
        disabled = []

        # Find each arg re to match
        for arg in self.args:
            arg_value = self.args[arg]
            if isinstance(arg_value, str) and arg_value.startswith('re:'):
                my_re = '^' + arg_value[3:] + '$'
                matched = False
                for key in state_keys:
                    res = re.search(my_re, key)
                    if res:
                        if len(res.groups()) > 0:
                            self.log('Regular expression argument {} matched {} with {}'.format(arg, my_re, res.group(1)))
                            self.args[arg] = res.group(1)
                            matched = True
                            break
                        else:
                            self.log('Regular expression argument {} Matched {} with {}'.format(arg, my_re, res.group(0)))
                            self.args[arg] = res.group(0)
                            matched = True
                            break
                if not matched:
                    self.log("WARN: Regular expression argument: {} unable to match {}, now will disable".format(arg, arg_value))
                    disabled.append(arg)

        # Remove unmatched keys
        for key in disabled:
            del self.args[key]

    def initialize(self):
        """
        Setup the app, called once each time the app starts
        """
        self.log("Predbat Startup")
        self.record_status("Startup")
        self.reset()
        self.auto_config()
        
        if SIMULATE and SIMULATE_LENGTH:
            for offset in range (0, SIMULATE_LENGTH, 30):
                now = datetime.now()
                midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
                minutes_now = int((now - midnight).seconds / 60)
                self.log("Simulated offset {}".format(offset))
                self.simulate_offset = offset + 30 - (minutes_now % 30)
                self.update_pred()
        else:
            # Run every N minutes aligned to the minute
            now = datetime.now()
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

            # First run is now
            self.run_in(self.run_time_loop, 0)

            # And then every N minutes
            self.run_every(self.run_time_loop, midnight, self.get_arg('run_every', 5) * 60)

    def run_time_loop(self, cb_args):
        """
        Called every N minutes
        """
        self.update_pred()
    