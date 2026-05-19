import locale
import os


def get_readable_file_size(db_name):
    """take the name of the database to monitor and return a human readable size"""
    # Set the locale to use space as the thousand separator
    locale.setlocale(locale.LC_ALL, "")

    # List of suffixes for different file sizes
    suffixes = ["B", "KB", "MB", "GB", "TB"]

    # Determine the appropriate suffix and scale
    suffix_index = 0
    file_size = float(os.path.getsize(db_name))

    while file_size >= 1024 and suffix_index < len(suffixes) - 1:
        file_size /= 1024
        suffix_index += 1

    # Format the file size with the appropriate suffix and thousand separator
    readable_size = f"{file_size:.2f} {suffixes[suffix_index]}"
    readable_size = (
        locale.format_string("%s", file_size, grouping=True)
        + f" {suffixes[suffix_index]}"
    )
    return readable_size
