# -------------------------------------------------------------------------------
# Name:        mbtiles2compactcache
# Purpose:     Build compact cache V2 bundles from MBTiles in SQLLite databases
#
# Author:      luci6974
#
# Created:     20/09/2016
# Modified:    04/05/2018,esristeinicke
#
#  Copyright 2016 Esri
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#       http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.?
#
# -------------------------------------------------------------------------------
#
# Converts .mbtile files to the esri Compact Cache V2 format
#
# Takes two arguments, the first one is the input .mbfile folder
# the second one being the output cache folder (_alllayers)
#
#
# Assumes that the input .mbtile files are named after the level.. (17.mbtile)
#
# Loops over columns and then row, in the order given by os.walk
# Keeps one bundle open in case the next tile fits in the same bundle
# In most cases this combination results in good performance
#
# It does not check the input tile format, and assumes that all
# the files are valid sqlite tile databases.  In other
# words, make sure there are no spurious files and folders under the input
# path.
#

import sys
import sqlite3
import os
import struct
import shutil
import time
import datetime
import re

# Bundle linear size in tiles
BSZ = 128
# Tiles per bundle
BSZ2 = BSZ ** 2
# Index size in bytes
IDXSZ = BSZ2 * 8

# Output path
output_path = None

# The curr_* variable are used for caching of open output bundles
# current bundle is kept open to reduce overhead
# TODO: Eliminate global variables
curr_bundle = None
# A bundle index list
# Array would be better, but it lacks 8 byte int support
curr_index = None
# Bundle file name without path or extension
curr_bname = None
# Current size of bundle file
curr_offset = int(0)
# max size of a tile in the current bundle
curr_max = 0


def init_bundle(file_name):
    """Create an empty V2 bundle file
    :param file_name: bundle file name
    """
    fd = open(file_name, "wb")
    # Empty bundle file header, lots of magic numbers
    header = struct.pack("<4I3Q6I",
                         3,  # Version
                         BSZ2,  # numRecords
                         0,  # maxRecord Size
                         5,  # Offset Size
                         0,  # Slack Space
                         64 + IDXSZ,  # File Size
                         40,  # User Header Offset
                         20 + IDXSZ,  # User Header Size
                         3,  # Legacy 1
                         16,  # Legacy 2
                         BSZ2,  # Legacy 3
                         5,  # Legacy 4
                         IDXSZ  # Index Size
                         )
    fd.write(header)
    # Write empty index.
    fd.write(struct.pack("<{}Q".format(BSZ2), *((0,) * BSZ2)))
    fd.close()


def cleanup():
    """
    Updates header and closes the current bundle
    """
    global curr_bundle, curr_bname, curr_index, curr_max, curr_offset
    curr_bname = None

    # Update the max rec size and file size, then close the file
    if curr_bundle is not None:
        curr_bundle.seek(8)
        curr_bundle.write(struct.pack("<I", curr_max))
        curr_bundle.seek(24)
        curr_bundle.write(struct.pack("<Q", curr_offset))
        curr_bundle.seek(64)
        curr_bundle.write(struct.pack("<{}Q".format(BSZ2), *curr_index))
        curr_bundle.close()

        curr_bundle = None


def open_bundle(row, col):
    """
    Make the bundle corresponding to the row and col current
    """
    global curr_bname, curr_bundle, curr_index, curr_offset, output_path, curr_max
    # row and column of top-left tile in the output bundle
    start_row = (row / BSZ) * BSZ
    start_col = (col / BSZ) * BSZ
    bname = "R{:04x}C{:04x}".format(start_row, start_col)
    #    bname = "R%(r)04xC%(c)04x" % {"r": start_row, "c": start_col}

    # If the name matches the current bundle, nothing to do
    if bname == curr_bname:
        return

    # Close the current bundle, if it exists
    cleanup()

    # Make the new bundle current
    curr_bname = bname
    # Open or create it, seek to end of bundle file
    fname = os.path.join(output_path, bname + ".bundle")

    # Create the bundle file if it didn't exist already
    if not os.path.exists(fname):
        init_bundle(fname)

    # Open the bundle
    curr_bundle = open(fname, "r+b")
    # Read the current max record size
    curr_bundle.seek(8)
    curr_max = int(struct.unpack("<I", curr_bundle.read(4))[0])
    # Read the index as longs in a list
    curr_bundle.seek(64)
    curr_index = list(struct.unpack("<{}Q".format(BSZ2),
                                    curr_bundle.read(IDXSZ)))
    # Go to end
    curr_bundle.seek(0, os.SEEK_END)
    curr_offset = curr_bundle.tell()


def add_tile(byte_buffer, row, col=None):
    """
    Add this tile to the output cache

    :param byte_buffer: input tile as byte buffer
    :param row: row number
    :param col: column number
    """
    global BSZ, curr_bundle, curr_max, curr_offset

    # Read the tile data
    tile = str(byte_buffer)
    tile_size = len(tile)

    # Write the tile at the end of the bundle, prefixed by size
    open_bundle(row, col)
    curr_bundle.write(struct.pack("<I", tile_size))
    curr_bundle.write(tile)
    # Skip the size
    curr_offset += 4

    # Update the index, row major
    curr_index[(row % BSZ) * BSZ + col % BSZ] = curr_offset + (tile_size << 40)
    curr_offset += tile_size

    # Update the current bundle max tile size
    curr_max = max(curr_max, tile_size)


def main():
    global output_path

    mb_tile_folder = sys.argv[1]
    cache_output_folder = sys.argv[2]

    # loop through all .mbtile files
    for root, dirs, files in os.walk(mb_tile_folder):
        # convert each .mbtile file to bundle cache
        # sore the list of files numerical
        for mbtile in sorted([x for x in files if x.endswith('.mbtile')],
                             key=lambda s: [int(c) if c.isdigit() else c for c in re.split('([0-9]+)', s)]):
            print('Working on file: {0}'.format(os.path.basename(mbtile)))
            # construct level folder name from .mbtile file name
            level = 'L' + '{:02d}'.format(int(os.path.splitext(os.path.basename(mbtile))[0]))
            level_folder = os.path.join(cache_output_folder, level)
            # get the level as int for later calculations
            level_int = int(os.path.splitext(os.path.basename(mbtile))[0])

            print('Bundles are written to folder: {0}'.format(level_folder))
            output_path = level_folder

            # create level folder if not exists
            if not os.path.exists(level_folder):
                os.makedirs(level_folder)
            else:
                # if exists, clean it up
                for sub_root, sub_dirs, sub_files in os.walk(level_folder):
                    for sub_dir in sub_dirs:
                        shutil.rmtree(sub_dir)
                    for sub_file in sub_files:
                        os.remove(os.path.join(sub_root, sub_file))

            # open the .mbtile as sqlite database
            database_file = os.path.join(mb_tile_folder, mbtile)
            database = sqlite3.connect(database_file)

            # create some indexes to speed up the process
            column_cursor = database.cursor()
            print('Creating column index...')
            column_cursor.execute('CREATE INDEX IF NOT EXISTS column_idx ON tiles(tile_column)')

            # get the total number of columns to work on
            # this in not necessary, used for timing info only
            print('Getting total number of columns to process...\t')
            number_of_columns = column_cursor.execute('SELECT count(distinct tile_column) FROM tiles').fetchone()[0]
            print('Total number of columns: {0}'.format(number_of_columns))

            # loop over each column
            column_cursor.execute('SELECT DISTINCT tile_column FROM tiles')
            start_time = time.time()
            current_column = 0
            current_percent = float(current_column) / float(number_of_columns) * 100
            print(' {0}% done - ETA: calculating'.format('{:2.2f}'.format(current_percent)))
            for column in column_cursor:
                current_column += 1

                # Process each row in sqlite database
                row_cursor = database.cursor()
                # calculate the maximum row number (there are 2^n rows and column at level n)
                # row numbering in .mbtile is reversed, row n must be converted to (max_rows -1 ) - n
                max_rows = 2 ** level_int - 1
                row_cursor.execute('SELECT * FROM tiles WHERE tile_column=?', (column[0],))
                for row in row_cursor:
                    add_tile(row[3], max_rows - int(row[2]), int(column[0]))

                # calculate ETA
                if current_column % 100 == 0:
                    current_column_time = (time.time() - start_time) / current_column * (
                            number_of_columns - current_column) / 60
                    current_percent = float(current_column) / float(number_of_columns) * 100
                    print('{0}% done - ETA {1} '.format(
                        '{:2.2f}'.format(current_percent),
                        str(datetime.datetime.now() + datetime.timedelta(minutes=current_column_time))[11:-7]))

            # close the database when finished
            database.close()
            print('Done - {0}\n'.format(str(datetime.datetime.now())[11:-7]))
            # cleanup open bundles
            cleanup()


if __name__ == '__main__':
    main()
