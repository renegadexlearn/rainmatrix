import os
import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from openpyxl import Workbook


TEST_DB_PATH = os.path.join(tempfile.gettempdir(), f"rainmatrix_test_{os.getpid()}.sqlite3")
os.environ["RAIN_CACHE_DB"] = TEST_DB_PATH

import run


class SolarPredictionTests(unittest.TestCase):
    def setUp(self):
        run.cache_init()
        with run.cache_connect() as conn:
            conn.execute("DELETE FROM rain_cache")
            conn.execute("DELETE FROM solar_production_history")

    def sample_hourly(self):
        today = run.safe_now_date_in_tz(run.DEFAULT_TZ)
        times = [datetime.combine(today, datetime.min.time()) + timedelta(hours=hour) for hour in range(24)]
        solar = [500.0 if time.hour == 12 else 0.0 for time in times]
        rain_solar = [60.0 if time.hour == 12 else 0.0 for time in times]
        return {
            "time": times,
            "solar": solar,
            "rain_solar": rain_solar,
            "rain_conservative_solar": [40.0 if time.hour == 12 else 0.0 for time in times],
            "rain_upper_solar": [100.0 if time.hour == 12 else 0.0 for time in times],
            "rain_consensus_used": [False] * len(times),
            "rain_model_count": 4,
            "precip": [2.0 if time.hour == 10 else 0.0 for time in times],
            "pop": [100 if time.hour == 10 else 0 for time in times],
            "rain_adjustment_precip": [2.0] * len(times),
            "rain_adjustment_pop": [100.0] * len(times),
            "rain_selected_model": ["GFS"] * len(times),
            "rain_interpretation": ["Heavy rain is likely; the lowest adjusted model is used."] * len(times),
            "rain_level": ["heavy"] * len(times),
            "rain_retained_factor": [0.12] * len(times),
            "cloud": [100 if time.hour == 12 else 0 for time in times],
            "solar_source": ["nowcast" if time.hour == 12 else "forecast" for time in times],
        }

    def empty_inverter_data(self):
        return {"hourly": {}, "daily": {}, "last_reading": None, "source": ""}

    def test_rain_adjustment_is_bounded_and_conservative(self):
        self.assertEqual(run.rain_retained_energy_factor(0, 0), 1.0)
        self.assertEqual(run.rain_retained_energy_factor(2, 100), run.RAIN_RETAINED_ENERGY_FLOOR)
        self.assertGreater(run.rain_retained_energy_factor(0, 50), run.RAIN_RETAINED_ENERGY_FLOOR)

    def test_smart_hourly_rain_ignores_trace_or_unlikely_rain(self):
        self.assertEqual(run.smart_hourly_rain_adjustment(0.01, 100), ("dry", 1.0))
        self.assertEqual(run.smart_hourly_rain_adjustment(5.0, 20), ("dry", 1.0))
        level, factor = run.smart_hourly_rain_adjustment(2.0, 100)
        self.assertEqual(level, "heavy")
        self.assertAlmostEqual(factor, run.RAIN_RETAINED_ENERGY_FLOOR, places=6)

    def test_dry_consensus_ignores_one_wet_model_outlier(self):
        day_start = datetime.combine(run.safe_now_date_in_tz(run.DEFAULT_TZ), datetime.min.time())
        morning = day_start + timedelta(hours=8)
        afternoon = day_start + timedelta(hours=14)
        primary = {
            "time": [morning, afternoon],
            "solar": [0.0, 500.0],
            "precip": [0.2, 0.0],
            "pop": [95, 0],
        }
        backup = {
            "time": [morning, afternoon],
            "solar": [0.0, 100.0],
            "precip": [0.0, 0.0],
            "pop": [0, 0],
            "cloud": [100, 100],
        }
        client = run.OpenMeteoClient()
        with patch.object(client, "hourly_forecast", return_value=backup):
            result = client.add_rain_model_scenarios(primary, 13.416, 121.161, run.DEFAULT_TZ)

        self.assertEqual(result["rain_solar"], [0.0, 100.0])
        self.assertEqual(result["rain_consensus_used"], [False, True])
        self.assertEqual(result["rain_conservative_solar"], [0.0, 100.0])
        self.assertEqual(result["rain_upper_solar"], [0.0, 500.0])
        self.assertEqual(result["rain_model_count"], 4)
        self.assertEqual(result["rain_adjustment_pop"], [0.0, 0.0])
        self.assertEqual(result["rain_adjustment_precip"], [0.0, 0.0])
        self.assertEqual(result["rain_selected_model"], ["ECMWF", "GFS"])
        self.assertEqual(result["rain_level"], ["dry", "dry"])

    def test_smart_forecast_can_choose_a_different_model_each_hour(self):
        day_start = datetime.combine(run.safe_now_date_in_tz(run.DEFAULT_TZ), datetime.min.time())
        morning = day_start + timedelta(hours=8)
        afternoon = day_start + timedelta(hours=14)
        primary = {
            "time": [morning, afternoon],
            "solar": [90.0, 400.0],
            "precip": [0.0, 2.0],
            "pop": [0, 100],
        }
        backups = []
        for index, solar in enumerate(([100.0, 300.0], [200.0, 200.0], [300.0, 100.0])):
            backups.append(
                {
                    "time": [morning, afternoon],
                    "solar": solar,
                    "precip": [0.0, 0.0 if index == 2 else 2.0],
                    "pop": [0, 0 if index == 2 else 100],
                    "cloud": [0, 100],
                }
            )
        client = run.OpenMeteoClient()
        with patch.object(client, "hourly_forecast", side_effect=backups):
            result = client.add_rain_model_scenarios(primary, 13.416, 121.161, run.DEFAULT_TZ)

        self.assertEqual(result["rain_solar"][0], 100.0)
        self.assertAlmostEqual(result["rain_solar"][1], 12.0, places=6)
        self.assertEqual(result["rain_consensus_used"], [True, True])
        self.assertEqual(result["rain_adjustment_pop"], [0.0, 100.0])
        self.assertEqual(result["rain_adjustment_precip"], [0.0, 2.0])
        self.assertEqual(result["rain_selected_model"], ["GFS", "GEM"])
        self.assertEqual(result["rain_level"], ["dry", "heavy"])
        self.assertEqual(result["rain_retained_factor"][0], 1.0)
        self.assertAlmostEqual(
            result["rain_retained_factor"][1],
            run.RAIN_RETAINED_ENERGY_FLOOR,
            places=6,
        )

    def test_satellite_nowcast_receives_rain_penalty_without_model_averaging(self):
        observed_hour = datetime.combine(
            run.safe_now_date_in_tz(run.DEFAULT_TZ), datetime.min.time()
        ) + timedelta(hours=10)
        primary = {
            "time": [observed_hour],
            "solar": [42.0],
            "precip": [4.0],
            "pop": [100],
            "solar_source": ["nowcast"],
        }
        backup = {
            "time": [observed_hour],
            "solar": [500.0],
            "precip": [0.0],
            "pop": [0],
            "cloud": [0],
        }

        client = run.OpenMeteoClient()
        with patch.object(client, "hourly_forecast", return_value=backup):
            result = client.add_rain_model_scenarios(primary, 13.416, 121.161, run.DEFAULT_TZ)

        self.assertAlmostEqual(result["rain_solar"][0], 5.04, places=6)
        self.assertAlmostEqual(result["rain_conservative_solar"][0], 5.04, places=6)
        self.assertAlmostEqual(result["rain_upper_solar"][0], 5.04, places=6)
        self.assertEqual(result["rain_consensus_used"], [False])
        self.assertEqual(result["rain_level"], ["heavy"])
        self.assertAlmostEqual(
            result["rain_retained_factor"][0],
            run.RAIN_RETAINED_ENERGY_FLOOR,
            places=6,
        )
        self.assertEqual(result["rain_selected_model"], ["Satellite nowcast"])
        self.assertIn("not site meter output", result["rain_interpretation"][0])

    def test_calibration_uses_median_after_three_completed_days(self):
        today = run.safe_now_date_in_tz(run.DEFAULT_TZ)
        with run.cache_connect() as conn:
            for days_ago, actual_kwh in ((3, 100.0), (2, 120.0), (1, 80.0)):
                conn.execute(
                    """
                    INSERT INTO solar_production_history
                      (site_id, production_date, actual_kwh, base_forecast_kwh, rain_forecast_kwh)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ("test-site", (today - timedelta(days=days_ago)).isoformat(), actual_kwh, 200.0, 100.0),
                )

        self.assertEqual(run.solar_calibration_factor("test-site", False, today), (0.5, 3))
        self.assertEqual(run.solar_calibration_factor("test-site", True, today), (1.0, 3))

    def test_inverter_workbook_integrates_power_into_hourly_kwh(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Time", "Solar Power（kW）", "Weather", "SOC（%）"])
        sheet.append(["2026/08/08 06:00:00", 0.0, None, 20])
        sheet.append(["2026/08/08 06:15:00", 0.0, "Heavy rain", None])
        sheet.append(["2026/08/08 06:20:00", 10.0, None, 20])
        sheet.append(["2026/08/08 06:40:00", 20.0, None, 20])
        sheet.append(["2026/08/08 07:00:00", 30.0, None, 20])
        handle = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        handle.close()
        self.addCleanup(lambda: os.path.exists(handle.name) and os.remove(handle.name))
        workbook.save(handle.name)

        result = run.inverter_actuals_for_file(handle.name)
        hour = datetime(2026, 8, 8, 6)
        self.assertAlmostEqual(result["hourly"][hour]["kwh"], 15.0, places=6)
        self.assertEqual(result["hourly"][hour]["coverage_minutes"], 60.0)
        self.assertFalse(result["daily"][hour.date()]["complete"])

    def test_site_route_uses_inverter_actual_for_covered_hour(self):
        site_id = "site_1785639554884"
        today = run.safe_now_date_in_tz(run.DEFAULT_TZ)
        actual_hour = datetime.combine(today, datetime.min.time()) + timedelta(hours=12)
        inverter_data = {
            "hourly": {actual_hour: {"kwh": 2.0, "coverage_minutes": 60.0}},
            "daily": {
                today: {
                    "kwh": 2.0,
                    "last_reading": actual_hour + timedelta(minutes=55),
                    "complete": False,
                }
            },
            "last_reading": actual_hour + timedelta(minutes=55),
            "source": "data.xlsx",
        }
        hourly = self.sample_hourly()
        hourly["solar"][13] = 500.0
        hourly["rain_solar"][13] = 60.0
        hourly["rain_conservative_solar"][13] = 40.0
        hourly["rain_upper_solar"][13] = 100.0
        fixed_now = datetime.combine(today, datetime.min.time()) + timedelta(hours=13, minutes=30)

        with patch.object(run, "safe_now_in_tz", return_value=fixed_now), patch.object(
            run, "inverter_actuals_for_file", return_value=inverter_data
        ), patch.object(run.OpenMeteoClient, "hybrid_solar_forecast", return_value=hourly):
            response = run.app.test_client().get(f"/solar-site?site={site_id}")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('<div class="bar-value">3</div>', html)
        self.assertIn("Inverter actual by now", html)
        self.assertIn("<strong>2.0 kWh</strong>", html)
        self.assertIn('"model": "Inverter actual"', html)
        self.assertIn('"effectLabel": "Measured"', html)
        self.assertIn("Measured inverter production from the workbook.", html)
        self.assertIn("Inverter feed: data.xlsx through", html)
        self.assertIn("Site calibration: learning (0/3 completed inverter days)", html)

    def test_site_route_switches_versions_and_renders_history_calendar(self):
        site_id = "site_1785639554884"
        client = run.app.test_client()
        with patch.object(run, "inverter_actuals_for_file", return_value=self.empty_inverter_data()), patch.object(
            run.OpenMeteoClient, "hybrid_solar_forecast", return_value=self.sample_hourly()
        ):
            standard = client.get(f"/solar-site?site={site_id}&rain=0")
            rainy = client.get(f"/solar-site?site={site_id}")

        standard_html = standard.get_data(as_text=True)
        rainy_html = rainy.get_data(as_text=True)
        self.assertEqual(standard.status_code, 200)
        self.assertEqual(rainy.status_code, 200)
        self.assertIn("Standard v1", standard_html)
        self.assertIn("Smart rain v7", rainy_html)
        self.assertIn('<div class="bar-value">10</div>', standard_html)
        self.assertIn('<div class="bar-value">1</div>', rainy_html)
        self.assertIn("Forecast &amp; Nowcast History", rainy_html)
        self.assertIn("Nowcast &middot; Smart rain v7", rainy_html)
        self.assertIn("Smart model chosen each hour from 4 weather models", rainy_html)
        self.assertIn("Expected range: 0.8-2.0 kWh", rainy_html)
        self.assertIn("Range 1-2 kWh", rainy_html)
        self.assertIn("Hourly Prediction &amp; Interpretation", rainy_html)
        self.assertIn("Heavy rain is likely; the lowest adjusted model is used.", rainy_html)
        self.assertIn("renderHourlyForecast", rainy_html)
        self.assertIn("Hourly rain penalty is still applied", rainy_html)
        self.assertIn('<button type="button" class="bar-slot day-bar"', rainy_html)
        self.assertIn('aria-controls="hourlyForecastCard"', rainy_html)
        self.assertIn("selectHourlyDay(index, true)", rainy_html)
        self.assertIn("Conditions Used For Model Selection", rainy_html)
        self.assertIn("At least 60% probability and 0.3 mm", rainy_html)
        self.assertIn("At least 80% probability and 1.0 mm", rainy_html)
        self.assertIn('"time": "6:00 am"', rainy_html)
        self.assertIn('"time": "6:00 pm"', rainy_html)
        self.assertNotIn('"time": "5:00 am"', rainy_html)
        self.assertNotIn('"time": "7:00 pm"', rainy_html)
        self.assertLess(
            rainy_html.index('id="hourlyForecastCard"'),
            rainy_html.index('id="batteryForecastCard"'),
        )
        self.assertNotIn("Save Actual", rainy_html)
        self.assertIn("Estimated by now", rainy_html)
        self.assertIn("Remaining today", rainy_html)
        self.assertIn('"stopDischargeSocPercent": 20.0', rainy_html)
        self.assertIn("Math.max(socKwh - reserveKwh, 0)", rainy_html)
        self.assertIn("const blockBatteryDischarge = shouldChargeFirst && chargeKwh > 0", rainy_html)
        self.assertIn("? 0\n      : Math.min(loadShortfallKwh, dischargeableKwh)", rainy_html)
        self.assertIn("Stop ${Math.round(stopDischargePct)}%", rainy_html)
        self.assertIn("Source: <strong>Smart rain v7 hourly</strong>", rainy_html)
        self.assertIn('id="showSolarPlot" type="checkbox" checked', rainy_html)
        self.assertIn('id="showConsumptionPlot" type="checkbox" checked', rainy_html)
        self.assertIn('drawHourlyEnergySeries("productionKwh", "#2563EB"', rainy_html)
        self.assertIn('drawHourlyEnergySeries("loadKwh", "#DC2626"', rainy_html)
        self.assertIn('}, "kWh"));', rainy_html)
        self.assertIn("const top = 34", rainy_html)
        self.assertNotIn('r: "3.5"', rainy_html)
        self.assertIn('id="hourlyDownloadBtn"', rainy_html)
        self.assertIn('id="batteryDownloadBtn"', rainy_html)
        self.assertIn('id="historyDownloadBtn"', rainy_html)
        self.assertIn('downloadSectionAsJpeg("hourlyForecastCard"', rainy_html)
        self.assertIn('downloadSectionAsJpeg("batteryForecastCard"', rainy_html)
        self.assertIn('"productionHistoryCard",', rainy_html)
        self.assertIn('canvas.toDataURL("image/jpeg", 0.95)', rainy_html)
        self.assertIn('target.classList.add("jpeg-exporting")', rainy_html)
        battery_payload = json.loads(
            re.search(r"const batteryForecast = (\{.*?\});\n", rainy_html).group(1)
        )
        hourly_payload = json.loads(
            re.search(r"const hourlyForecast = (\{.*?\});\n", rainy_html).group(1)
        )
        battery_points = battery_payload["days"][0]["points"]
        hourly_by_time = {
            row["time"]: row["expectedKwh"]
            for row in hourly_payload["days"][0]["rows"]
        }
        self.assertTrue(battery_points[0]["isInitial"])
        self.assertEqual(battery_points[0]["time"], "6am")
        self.assertEqual(battery_points[-1]["time"], "6pm")
        for point in battery_points[1:]:
            self.assertAlmostEqual(
                point["productionKwh"],
                hourly_by_time[point["forecastHour"]],
                places=3,
            )
        calendar_day_count = rainy_html.count('class="calendar-day')
        self.assertGreaterEqual(calendar_day_count, 35)
        self.assertEqual(calendar_day_count % 7, 0)

    def test_history_saves_the_latest_query_instead_of_manual_actuals(self):
        site_id = "site_1785639554884"
        production_day = run.safe_now_date_in_tz(run.DEFAULT_TZ)
        client = run.app.test_client()
        first_hourly = self.sample_hourly()
        updated_hourly = self.sample_hourly()
        updated_hourly["solar"][12] = 600.0

        with patch.object(run, "inverter_actuals_for_file", return_value=self.empty_inverter_data()), patch.object(
            run.OpenMeteoClient, "hybrid_solar_forecast", return_value=first_hourly
        ):
            client.get(f"/solar-site?site={site_id}")
        with patch.object(run, "inverter_actuals_for_file", return_value=self.empty_inverter_data()), patch.object(
            run.OpenMeteoClient, "hybrid_solar_forecast", return_value=updated_hourly
        ):
            client.get(f"/solar-site?site={site_id}")

        with run.cache_connect() as conn:
            base_kwh, source_type = conn.execute(
                "SELECT base_display_kwh, source_type FROM solar_production_history WHERE site_id=? AND production_date=?",
                (site_id, production_day.isoformat()),
            ).fetchone()
        self.assertAlmostEqual(base_kwh, 11.7936, places=3)
        self.assertEqual(source_type, "nowcast")
        self.assertEqual(client.post("/solar-site/actual").status_code, 404)

    def test_admin_exposes_stop_discharge_soc_default(self):
        admin_html = run.app.test_client().get("/admin").get_data(as_text=True)
        self.assertIn("Stop Discharge SOC %", admin_html)
        self.assertIn("site.stop_discharge_soc_percent = 20", admin_html)

    def test_history_schema_repairs_itself_if_table_is_missing(self):
        with run.cache_connect() as conn:
            conn.execute("DROP TABLE solar_production_history")

        with patch.object(run, "inverter_actuals_for_file", return_value=self.empty_inverter_data()), patch.object(
            run.OpenMeteoClient, "hybrid_solar_forecast", return_value=self.sample_hourly()
        ):
            response = run.app.test_client().get("/solar-site?site=site_1785639554884")

        self.assertEqual(response.status_code, 200)
        with run.cache_connect() as conn:
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='solar_production_history'"
            ).fetchone()
        self.assertEqual(table, ("solar_production_history",))


if __name__ == "__main__":
    unittest.main()
