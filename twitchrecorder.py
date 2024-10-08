import datetime
import enum
import getopt
import logging
import os
import subprocess
import sys
import shutil
import time
import threading
import requests
import re

import config

class TwitchResponseStatus(enum.Enum):
    ONLINE = 0
    OFFLINE = 1
    NOT_FOUND = 2
    UNAUTHORIZED = 3
    ERROR = 4

class TwitchRecorder:
    def __init__(self):
        # global configuration
        self.ffmpeg_path = "ffmpeg"
        self.disable_ffmpeg = False
        self.refresh = 15
        self.root_path = config.root_path

        # user configuration
        self.username = config.username
        self.quality = "best"

        # twitch configuration
        self.client_id = config.client_id
        self.client_secret = config.client_secret
        self.token_url = "https://id.twitch.tv/oauth2/token?client_id=" + self.client_id + "&client_secret=" \
                         + self.client_secret + "&grant_type=client_credentials"
        self.url = "https://api.twitch.tv/helix/streams"
        self.access_token = self.fetch_access_token()
        self.stream_start_time = None

        # Discord webhook configuration
        self.discord_webhook_url = config.discord_webhook_url

    def fetch_access_token(self):
        token_response = requests.post(self.token_url, timeout=15)
        token_response.raise_for_status()
        token = token_response.json()
        return token["access_token"]

    def send_discord_message(self, title, description, status_color):
        payload = {
            "embeds": [
                {
                    "title": title,
                    "description": description,
                    "color": status_color
                }
            ]
        }
        requests.post(self.discord_webhook_url, json=payload)

    def send_stream_start_notification(self):
        message = f"Stream for [{self.username}](https://twitch.tv/{self.username}) has started!"
        self.send_discord_message("Stream Started", message, 65280)  # Green color

    def send_stream_stop_notification(self):
        message = f"Stream for [{self.username}](https://twitch.tv/{self.username}) has stopped."
        self.send_discord_message("Stream Stopped", message, 16711680)  # Red color

    def send_stream_failure_notification(self, error_message):
        message = f"Stream for [{self.username}](https://twitch.tv/{self.username}) has encountered an error:\n{error_message}"
        self.send_discord_message("Stream Error", message, 16776960)  # Yellow color

    def run(self):
        # Start a thread for checking stream status
        stream_check_thread = threading.Thread(target=self.check_stream_continuously)
        stream_check_thread.start()

        # Process recorded files
        self.process_recorded_files()

    def check_stream_continuously(self):
        while True:
            status, info = self.check_user()
            if status == TwitchResponseStatus.NOT_FOUND:
                logging.error("username not found, invalid username or typo")
                time.sleep(self.refresh)
            elif status == TwitchResponseStatus.ERROR:
                error_message = "Unexpected error. Restarting script."
                logging.error(error_message)
                self.send_stream_failure_notification(error_message)
                time.sleep(2)
                sys.exit()
            elif status == TwitchResponseStatus.OFFLINE:
                if self.stream_start_time:
                    self.send_stream_stop_notification()
                    self.stream_start_time = None
                logging.info("%s currently offline, checking again in %s seconds", self.username, self.refresh)
                time.sleep(self.refresh)
            elif status == TwitchResponseStatus.UNAUTHORIZED:
                error_message = "Unauthorized, will attempt to log back in immediately"
                logging.info(error_message)
                self.access_token = self.fetch_access_token()
                self.send_stream_failure_notification(error_message)
            elif status == TwitchResponseStatus.ONLINE:
                self.send_stream_start_notification()
                logging.info("%s online, stream recording in session", self.username)

                channels = info["data"]
                channel = next(iter(channels), None)
                filename = self.username + " - " + datetime.datetime.now() \
                    .strftime("%y%m%d %Hh%Mm%Ss") + " - " + channel.get("title") + ".mp4"

                # clean filename from unnecessary characters
                filename = "".join(x for x in filename if x.isalnum() or x in [" ", "-", "_", "."])

                recorded_filename = os.path.join(self.root_path, "recorded", self.username, filename)
                processed_filename = os.path.join(self.root_path, "processed", self.username, filename)

                # start streamlink process
                subprocess.call(
                    ["streamlink", "--twitch-disable-ads", "twitch.tv/" + self.username, self.quality,
                     "-o", recorded_filename])

                logging.info("recording stream is done, processing video file")
                if os.path.exists(recorded_filename) is True:
                    self.process_recorded_file(recorded_filename, processed_filename)
                else:
                    logging.info("skip fixing, file not found")

                logging.info("processing is done, going back to checking...")
                time.sleep(self.refresh)

    def process_recorded_files(self):
        # path to recorded stream
        recorded_path = os.path.join(self.root_path, "recorded", self.username)
        # path to finished video, errors removed
        processed_path = os.path.join(self.root_path, "processed", self.username)

        # create directory for recordedPath and processedPath if not exist
        if os.path.isdir(recorded_path) is False:
            os.makedirs(recorded_path)
        if os.path.isdir(processed_path) is False:
            os.makedirs(processed_path)

        try:
            video_list = [f for f in os.listdir(recorded_path) if os.path.isfile(os.path.join(recorded_path, f))]
            if len(video_list) > 0:
                logging.info("processing previously recorded files")
            for f in video_list:
                recorded_filename = os.path.join(recorded_path, f)
                processed_filename = os.path.join(processed_path, f)
                self.process_recorded_file(recorded_filename, processed_filename)
        except Exception as e:
            logging.error(e)

    def process_recorded_file(self, recorded_filename, processed_filename):
        if self.disable_ffmpeg:
            logging.info("moving: %s", recorded_filename)
            shutil.move(recorded_filename, processed_filename)
        else:
            logging.info("fixing %s", recorded_filename)
            self.ffmpeg_copy_and_fix_errors(recorded_filename, processed_filename)

    def ffmpeg_copy_and_fix_errors(self, recorded_filename, processed_filename):
        try:
            subprocess.call(
                [self.ffmpeg_path, "-err_detect", "ignore_err", "-i", recorded_filename, "-c", "copy",
                 processed_filename])
            os.remove(recorded_filename)
        except Exception as e:
            logging.error(e)

    def check_user(self):
        info = None
        status = TwitchResponseStatus.ERROR
        try:
            headers = {"Client-ID": self.client_id, "Authorization": "Bearer " + self.access_token}
            r = requests.get(self.url + "?user_login=" + self.username, headers=headers, timeout=15)
            r.raise_for_status()
            info = r.json()
            if info is None or not info["data"]:
                status = TwitchResponseStatus.OFFLINE
            else:
                status = TwitchResponseStatus.ONLINE
                # Store stream start time
                if not self.stream_start_time:
                    self.stream_start_time = datetime.datetime.utcnow().strftime("%Y%m%d")
        except requests.exceptions.RequestException as e:
            if e.response:
                if e.response.status_code == 401:
                    status = TwitchResponseStatus.UNAUTHORIZED
                if e.response.status_code == 404:
                    status = TwitchResponseStatus.NOT_FOUND
        return status, info

def main(argv):
    twitch_recorder = TwitchRecorder()
    usage_message = "twitch-recorder.py -u <username> -q <quality>"
    logging.basicConfig(filename="twitch-recorder.log", level=logging.INFO)
    logging.getLogger().addHandler(logging.StreamHandler())

    try:
        opts, args = getopt.getopt(argv, "hu:q:l:", ["username=", "quality=", "log=", "logging=", "disable-ffmpeg"])
    except getopt.GetoptError:
        print(usage_message)
        sys.exit(2)
    for opt, arg in opts:
        if opt == "-h":
            print(usage_message)
            sys.exit()
        elif opt in ("-u", "--username"):
            twitch_recorder.username = arg
        elif opt in ("-q", "--quality"):
            twitch_recorder.quality = arg
        elif opt in ("-l", "--log", "--logging"):
            logging_level = getattr(logging, arg.upper(), None)
            if not isinstance(logging_level, int):
                raise ValueError("invalid log level: %s" % logging_level)
            logging.basicConfig(level=logging_level)
            logging.info("logging configured to %s", arg.upper())
        elif opt == "--disable-ffmpeg":
            twitch_recorder.disable_ffmpeg = True
            logging.info("ffmpeg disabled")

    twitch_recorder.run()

if __name__ == "__main__":
    main(sys.argv[1:])
