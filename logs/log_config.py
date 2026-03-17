import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
import os

class DateRotatingFileHandler(RotatingFileHandler):
    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        
        # Create a new log file name with the current date and time
        dfn = self.baseFilename + datetime.now().strftime("_%Y-%m-%d_%H-%M-%S")
        
        # Ensure the new log file name does not already exist
        if os.path.exists(dfn):
            os.remove(dfn)
        
        # Rotate the files
        self.rotate(self.baseFilename, dfn)
        
        # Open a new log file
        if not self.delay:
            self.stream = self._open()



# Configure Apolo trader logger
apolo_logger = logging.getLogger('apolo_logger')
if not apolo_logger.hasHandlers():
    apolo_logger.setLevel(logging.DEBUG)

    # Apolo file handler
    apolo_handler = DateRotatingFileHandler(
        os.path.join(os.path.dirname(__file__), 'apolo.log'), 
        maxBytes=5*1024*1024, 
        backupCount=5  # Changed from 0 to keep some backups
    )
    apolo_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    apolo_handler.setFormatter(formatter)
    apolo_logger.addHandler(apolo_handler)  # ✅ CORRECT - adding to apolo logger

# Backward-compatible exported name used across the project.
apolo_trader_logger = apolo_logger