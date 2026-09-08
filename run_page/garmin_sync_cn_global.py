"""
Python 3 API wrapper for Garmin Connect to get your statistics.
Copy most code from https://github.com/cyberjunky/python-garminconnect
"""

import argparse
import asyncio
import os
import sys


from config import FIT_FOLDER, GPX_FOLDER, JSON_FILE, SQL_FILE
from garmin_sync import Garmin, get_downloaded_ids
from garmin_sync import download_new_activities
from utils import make_activities_file

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "cn_secret_string", nargs="?", help="secret_string fro get_garmin_secret.py"
    )
    parser.add_argument(
        "global_secret_string", nargs="?", help="secret_string fro get_garmin_secret.py"
    )
    parser.add_argument(
        "--only-run",
        dest="only_run",
        action="store_true",
        help="if is only for running",
    )
    parser.add_argument(
        "--reverse",
        dest="reverse",
        action="store_true",
        help="reverse direction: sync from Global to CN (default: CN to Global)",
    )

    options = parser.parse_args()
    secret_string_cn = options.cn_secret_string
    secret_string_global = options.global_secret_string
    is_only_running = options.only_run
    if secret_string_cn is None or secret_string_global is None:
        print("Missing argument nor valid configuration file")
        sys.exit(1)

    # direction: default CN -> Global; --reverse flips to Global -> CN
    if options.reverse:
        download_secret, download_domain = secret_string_global, "COM"
        upload_secret, upload_domain = secret_string_cn, "CN"
        # each direction keeps its own run_id set so dedup doesn't cross-contaminate
        fit_folder = os.path.join(os.path.dirname(FIT_FOLDER), "FIT_OUT_GLOBAL")
        gpx_folder = os.path.join(os.path.dirname(GPX_FOLDER), "GPX_OUT_GLOBAL")
    else:
        download_secret, download_domain = secret_string_cn, "CN"
        upload_secret, upload_domain = secret_string_global, "COM"
        fit_folder = FIT_FOLDER
        gpx_folder = GPX_FOLDER

    # Step 1:
    # Sync all activities from the source account to the target account in FIT format
    # If the activity is manually imported with a GPX, the GPX file will be synced

    # make gpx or fit dir
    if not os.path.exists(fit_folder):
        os.mkdir(fit_folder)
    if not os.path.exists(gpx_folder):
        os.mkdir(gpx_folder)

    # load synced activity list
    downloaded_fit = get_downloaded_ids(fit_folder)
    downloaded_gpx = get_downloaded_ids(gpx_folder)
    downloaded_activity = list(set(downloaded_fit + downloaded_gpx))

    loop = asyncio.get_event_loop()
    future = asyncio.ensure_future(
        download_new_activities(
            download_secret,
            download_domain,
            downloaded_activity,
            is_only_running,
            fit_folder,
            "fit",
            gpx_folder,
        )
    )
    loop.run_until_complete(future)
    new_ids, id2title = future.result()

    to_upload_files = []
    for i in new_ids:
        if os.path.exists(os.path.join(fit_folder, f"{i}.fit")):
            # upload fit files
            to_upload_files.append(os.path.join(fit_folder, f"{i}.fit"))
        elif os.path.exists(os.path.join(gpx_folder, f"{i}.gpx")):
            # upload gpx files which are manually uploaded to garmin connect
            to_upload_files.append(os.path.join(gpx_folder, f"{i}.gpx"))

    print("Files to sync:" + " ".join(to_upload_files))
    garmin_upload_client = Garmin(
        upload_secret,
        upload_domain,
        is_only_running,
    )
    loop = asyncio.get_event_loop()
    future = asyncio.ensure_future(
        garmin_upload_client.upload_activities_files(to_upload_files)
    )
    loop.run_until_complete(future)

    # Step 2:
    # Generate track from fit/gpx file. Only the forward CN -> Global direction feeds
    # the running_page display; reverse mode is a pure mirror, so skip regenerating.
    if not options.reverse:
        make_activities_file(
            SQL_FILE, gpx_folder, JSON_FILE, file_suffix="gpx", activity_title_dict=id2title
        )
        make_activities_file(
            SQL_FILE, fit_folder, JSON_FILE, file_suffix="fit", activity_title_dict=id2title
        )
