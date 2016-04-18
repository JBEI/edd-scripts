"""
A script that searches EDD and ICE and corrects inconsistencies in the ICE experiment links to EDD.
This script was authored to address a known need due to the path/scheduling of the EDD/ICE
development processes, but some or all of it should also be maintained for future use (SYNBIO-1190)

This script is designed to run while EDD and ICE are both up, so there is a small chance
that users of one or both systems are modifying relevant data during the run.  The script is
designed to minimize the chances of concurrent modifications affecting the end results, but it does
not take explicit (and complicated / effort-intensive) steps to protect against them.
In the worst case, in the unlikely event that a race condition affects the results, a second run
of the script should detect and correct remaining inconsistencies, with a small chance of creating
new ones.

It's safest to schedule runs of this script during off-hours when users are less likely to be
making changes.
"""

from __future__ import unicode_literals
from __future__ import division

####################################################################################################
# set default source for ICE settings BEFORE importing any code from jbei.ice.rest.ice. Otherwise,
# code in that module will attempt to look for a django settings module and fail if django isn't
# installed in the current virtualenv
import os
from collections import OrderedDict

os.environ.setdefault('ICE_SETTINGS_MODULE', 'jbei.edd.rest.scripts.settings')
####################################################################################################


####################################################################################################
# configure an INFO-level logger just for our code (avoids INFO messages from supporting frameworks)
# Note: needs to be before importing other modules that get a logger reference
####################################################################################################
import logging
from requests.exceptions import HTTPError
import sys
LOG_LEVEL = logging.DEBUG
# redirect to stdout so log messages appear sequentially
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(LOG_LEVEL)
formatter = logging.Formatter('%(asctime)s %(name)s %(levelname)s: %(message)s')
console_handler.setFormatter(formatter)

# set a higher log level for supporting frameworks to help with debugging
# TODO: comment out?
root_logger = logging.getLogger('root')
root_logger.setLevel(LOG_LEVEL)
root_logger.addHandler(console_handler)

jbei_root_logger = logging.getLogger('jbei')
jbei_root_logger.setLevel(LOG_LEVEL)
jbei_root_logger.addHandler(console_handler)

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)
logger.addHandler(console_handler)

# TODO: why isn't this inherited from root? without these lines, get "No handlers could be found for
#  logger "jbei.edd.rest.edd""
# edd_logger = logging.getLogger('jbei.edd.rest.edd')
# edd_logger.setLevel(logging.ERROR)
# edd_logger.addHandler(console_handler)
####################################################################################################

import argparse
import arrow
import locale
from urlparse import urlparse
import requests
from jbei.edd.rest.edd import (EddApi, EddSessionAuth, Strain as EddStrain)
from jbei.ice.rest.ice import (IceApi, SessionAuth as IceSessionAuth, Strain as IceStrain, STRAIN)
from jbei.rest.utils import is_url_secure
from jbei.utils import to_human_relevant_delta, UserInputTimer, session_login
import re
from requests import ConnectionError
from settings import EDD_URL, ICE_URL

locale.setlocale(locale.LC_ALL, b'en_US')

####################################################################################################
# Performance tuning parameters
####################################################################################################
# process large-ish result batches in the hope that we've stumbled on an good size to make
# processing efficient in aggregate
EDD_RESULT_PAGE_SIZE = ICE_RESULT_PAGE_SIZE = 100
EDD_REQUEST_TIMEOUT = ICE_REQUEST_TIMEOUT = (10, 10)  # timeouts in seconds = (connection, response)

####################################################################################################
# Version-specific settings. These control functions that should only be executed during the initial
# run of this script, then likely not used again afterward.
####################################################################################################
DEBUG = True
PROCESS_ICE_ENTRIES = True  # Examine all ICE entries during the initial run, including those NOT
                            # referenced from EDD. There's been a lack of updates from EDD, as well
                            # as a lack of enforcement that EDD can only consume entries of type
                            # strain from ICE.
search_ice_part_types = None  # in the future, if any, consider just [STRAIN]

####################################################################################################

SEPARATOR_CHARS = 75
OUTPUT_SEPARATOR = ''.join(['*' for index in range(1, SEPARATOR_CHARS)])
fill_char = b'.'

NOT_PROCESSED_OUTCOME = 'NOT_PROCESSED'
REMOVED_DEVELOPMENT_URL_OUTCOME = 'REMOVED_DEV_URL'
REMOVED_TEST_URL_OUTCOME = 'REMOVED_TEST_URL'
UPDATED_PERL_URL_OUTCOME = 'UPDATED_PERL_URL'
REMOVED_BAD_STUDY_LINK = 'REMOVED_NON_EXISTENT_STUDY_LINK'

class Performance(object):
    def __init__(self):
        #######################################
        # time tracking
        #######################################
        self._overall_start_time = arrow.utcnow()
        self._overall_end_time = None
        self._total_time = None
        self.ice_communication_time = None
        self.edd_communication_time = None
        self.ice_entry_scan_start_time = None
        self.ice_entry_scan_time = None
        self.edd_strain_scan_time = None

        #######################################
        # other time-related statistics
        #######################################
        self.max_strain_processing_performance = None
        self.min_strain_processing_performance = None

    def completed_edd_strain_scan(self):
        self.edd_strain_scan_time = arrow.utcnow() - self._overall_start_time

    def started_ice_entry_scan(self):
        self.ice_entry_scan_start_time = arrow.utcnow()


    @property
    def overall_end_time(self):
        return self._overall_end_time

    @overall_end_time.setter
    def overall_end_time(self, value):
        self._overall_end_time = value
        self._total_time = self.overall_end_time - self._overall_start_time
        if self.ice_entry_scan_start_time:
            self.ice_entry_scan_time = value - self.ice_entry_scan_start_time

    def print_summary(self):
        print(OUTPUT_SEPARATOR)
        print('Performance Summary')
        print(OUTPUT_SEPARATOR)

        # build up a dictionary of result titles -> values
        total_runtime = self._overall_end_time - self._overall_start_time
        print('Total run time: %s' % to_human_relevant_delta(total_runtime.total_seconds()))
        values_dict = OrderedDict()
        values_dict['EDD strain scan duration'] = to_human_relevant_delta(
                self.edd_strain_scan_time.total_seconds())
        values_dict['ICE entry scan duration:'] = (to_human_relevant_delta(
                self.ice_entry_scan_time.total_seconds())
        if self.ice_entry_scan_time else 'Not performed')
        values_dict['Total EDD communication time'] = to_human_relevant_delta(
                self.edd_communication_time.total_seconds())
        values_dict['Total ICE communication time'] = to_human_relevant_delta(
                self.ice_communication_time.total_seconds())

        # compute column widths for readable display
        space = 2
        title_col_width = max(len(title) for title in values_dict.keys()) + space
        value_col_width = max(len(value) for value in values_dict.values()) + space

        for title, value in values_dict.items():
            indented_title = "\t\t%s" % title.ljust(title_col_width, fill_char)
            print(fill_char.join((indented_title, value.rjust(value_col_width, fill_char))))



class StrainProcessingPerformance:
    def __init__(self, start_time, starting_ice_communication_delta,
                 starting_edd_communication_delta):

        self.start_time = start_time
        self._total_time = None
        self._end_time = None

        self.edd_study_search_time = None
        self.strain_processing_start = start_time

        self._starting_ice_communication_delta = starting_ice_communication_delta
        self._starting_edd_communication_delta = starting_edd_communication_delta
        self._edd_communication_delta = None
        self._ice_communication_delta = None

        self.ice_link_search_delta = None
        self.ice_link_cache_lifetime = None
        self.links_updated = 0
        self.links_removed = 0
        self.links_skipped = 0
        self.links_unprocessed = 0
        self.studies_unprocessed = 0

    def print_summary(self):

        ############################################################################################
        # Print a summary of runtime
        ############################################################################################

        print('Single-strain run time: %s' % to_human_relevant_delta(
                self._total_time.total_seconds()))
        # print('\tTotal EDD communication: %s' % to_human_relevant_delta(
        #         self._edd_communication_delta.total_seconds()))
        # print('\tTotal ICE communication: %s' % to_human_relevant_delta(
        #         self._ice_communication_delta.total_seconds()))
        # print('\tICE strain experiments cache lifetime: %s' % to_human_relevant_delta(
        #         self.ice_link_cache_lifetime.total_seconds()))
        # print('\tICE links processed: %d' % (self.links_updated + self.links_removed +
        #                                      self.links_skipped))

        if self.links_unprocessed:
            print('\tICE links UNprocessed: %d' % self.links_unprocessed)


    @property
    def end_time(self):
        return self._end_time

    def set_end_time(self, value, end_edd_communication_delta, end_ice_communation_delta):
        self._end_time = value
        self._total_time = self.end_time - self.start_time
        self._edd_communication_delta = end_edd_communication_delta - \
            self._starting_edd_communication_delta
        self._ice_communication_delta = end_ice_communation_delta - \
            self._starting_ice_communication_delta

from jbei.ice.rest.ice import DEFAULT_RESULT_LIMIT as DEFAULT_ICE_RESULT_LIMIT


class IceTestStub(IceApi):

    """
    A variant of IceAPI that masks all capability to change data in ICE. All attempts to change
    data return success, but don't actually make any changes
    """
    def unlink_entry_from_study(self, ice_entry_id, study_id, study_url, logger):
        return True

    def link_entry_to_study(self, ice_entry_id, study_id, study_url, study_name, logger,
                            old_study_name=None, old_study_url=None):
        pass

    def remove_experiment_link(self, ice_entry_id, link_id):
        pass

edd_hostname = urlparse(EDD_URL).hostname
ice_hostname = urlparse(ICE_URL).hostname

PERL_STUDY_URL_PATTERN = re.compile(r'^http(?:s?)://%s/study\.cgi\?studyid=(?P<study_id>\d+)/?$' %
                       re.escape(edd_hostname), re.IGNORECASE)
STUDY_URL_PATTERN = re.compile(r'^http(?:s?)://%s/study/(?P<study_id>\d+)/?$' %
                               re.escape(edd_hostname), re.IGNORECASE)


def build_perl_study_url(study_pk, https=False):
    scheme = 'https' if https else 'http'
    return '%(scheme)://edd.jbei.org/Study.cgi?studyID=%(study_pk)s' % {
        'scheme': scheme,
        'study_pk': study_pk
    }


def is_development_url(url):
    url_parts = urlparse(url)
    hostname = url_parts.hostname.lower()

    developer_machine_names = ['gbirkel-mr.dhcp.lbl.gov', 'mforrer-mr.dhcp.lbl.gov',
                               'jeads-mr.dhcp.lbl.gov', 'wcmorrell-mr.dhcp.lbl.gov', ]

    if hostname in developer_machine_names:
        return True

    suspicious_suffix = '.dhcp.lbl.gov'

    if hostname.endswith(suspicious_suffix):
        logger.warning("""Link URL %(url)s ends with suspicious suffix "%(suspicious_suffix)s,"""
                       """but wasn't detected to link to a developer\'s machine""" % {
            'url': url,
            'suspicious_suffix': suspicious_suffix
        })

EDD_TEST_HOSTNAME_PATTERN = re.compile(r'edd-test(?:\d?).jbei.org', re.IGNORECASE)
ICE_TEST_HOSTNAME_PATTERN = re.compile(r'registry-test(?:\d?).jbei.org', re.IGNORECASE)


def is_edd_test_instance_url(url):
    """
    Tests whether the input URL refers to a test deployment of EDD as deployed at JBEI
    """
    url_parts = urlparse(url)
    hostname = url_parts.hostname
    return bool(EDD_TEST_HOSTNAME_PATTERN.match(hostname))


def is_ice_test_instance_url(url):
    """
    Tests whether the input URL refers to a test deployment of ICE as deployed at JBEI
    """
    url_parts = urlparse(url)
    hostname = url_parts.hostname
    return bool(ICE_TEST_HOSTNAME_PATTERN.match(hostname))


def is_ice_admin_user(ice, username):
    try:
        page_result = ice.search_users(search_string=username)
        user_email_pattern = re.compile('%s@.+' % username, re.IGNORECASE)

        while page_result:

            for user in page_result.results:
                if user_email_pattern.match(user.email):
                    return user.is_admin

            if page_result.next_page:
                page_result = ice.search_users(query_url=page_result.next_page)
    except HTTPError as h:
        if h.code == 403:
            return False

        if h.code == 500:  # work around ICE's imprecise return codes,at the cost of
                           # masking actual internal server errors (SYNBIO-1359)
            return False

    return None


def is_aborted():
        # TODO: placeholder for aborting early. Instead use Celery's AbortableTask, assuming the
        # documentation that states it doesn't support our back-end is outdated (
        #http://docs.celeryproject.org/en/latest/reference/celery.contrib.abortable.html )
        return False


class ProcessingSummary:
    def __init__(self):
        self._total_ice_entries_processed = 0
        self._total_edd_strains_found = 0
        self._total_ice_entries_found = 0
        self._existing_links_processed = 0
        self._invalid_links_pruned = 0
        self._development_links_pruned = 0
        self._test_links_pruned = 0
        self._unmaintained_links_renamed = 0
        self._valid_links_skipped = 0
        self._missing_links_created = 0
        self._non_strain_ice_parts_referenced = 0
        self._perl_links_updated = 0
        self._skipped_external_links = 0

        self._previously_processed_strains_skipped = 0

        self._orphaned_edd_strains = []
        self._stepchild_edd_strains = []
        self._non_strain_ice_parts_referenced = []

        self._processed_edd_strain_uuids = {}

    ################################################################################################
    # Read-only properties where we want to force additional data capture
    ################################################################################################

    def skipped_external_link(self, ice_entry, link):
        self._skipped_external_links += 1

        logger.warning('Leaving external ICE link to %(link_url)s in place from ICE part '
                       '%(part_id)s (uuid %(entry_uuid)s)' % {
                           'link_url': link.url,
                           'part_id': ice_entry.part_id,
                           'entry_uuid': ice_entry.uuid, })

    @property
    def orphaned_edd_strain_count(self):
        return len(self._orphaned_edd_strains)

    @property
    def total_edd_strains_processed(self):
        return len(self._processed_edd_strain_uuids)

    @property
    def total_ice_entries_processed(self):
        return self._total_ice_entries_processed

    @property
    def valid_links_skipped(self):
        return self._valid_links_skipped

    @property
    def unmaintained_links_renamed(self):
        return self._unmaintained_links_renamed

    @property
    def development_links_pruned(self):
        return self._development_links_pruned

    @property
    def invalid_links_removed(self):
        return self._removed_invalid_links


    ################################################################################################

    def is_edd_strain_processed(self, strain_uuid):
        return self._processed_edd_strain_uuids.get(strain_uuid, False)

    @property
    def total_edd_strains_found(self):
        return self._total_edd_strains_found

    @total_edd_strains_found.setter
    def total_edd_strains_found(self, found_count):
        self._total_edd_strains_found = found_count

    @property
    def total_ice_entries_found(self):
        return self._total_ice_entries_found

    @total_ice_entries_found.setter
    def total_ice_entries_found(self, entries_found):
        self._total_ice_entries_found = entries_found

    def removed_development_link(self, ice_entry, experiment_link):
        self._existing_links_processed += 1
        self._development_links_pruned += 1

        logger.info('Removed development link %(link_url)s from ICE entry %(entry_uuid)s' % {
                    'link_url': experiment_link.url,
                    'entry_uuid': ice_entry.uuid, })

    def removed_test_link(self, ice_entry, experiment_link):
        self._existing_links_processed += 1
        self._test_links_pruned += 1

        logger.info('Removed test link %(link_url)s from ICE entry %(entry_uuid)s' % {
                    'link_url': experiment_link.url,
                    'entry_uuid': ice_entry.uuid, })

    def removed_invalid_link(self, ice_entry, experiment_link):
        self._existing_links_processed += 1
        self._invalid_links_pruned += 1

        logger.info('Removed invalid link %(link_url)s from ICE entry %(entry_uuid)s' % {
                    'link_url': experiment_link.url,
                    'entry_uuid': ice_entry.uuid, })

    def renamed_unmaintained_link(self, ice_entry, existing_link, new_link_name):
        self._existing_links_processed += 1
        self._unmaintained_links_renamed += 1

        logger.info('Renamed unmaintained link from %(old_name)s to %(new_name)s to %(link_url)s '
                    'from ICE entry %(entry_uuid)s' % {
                        'old_name': existing_link.name,
                        'new_name': new_link_name,
                        'link_url': existing_link.url,
                        'entry_uuid': ice_entry.uuid, })

    def skipped_valid_link(self, ice_entry, experiment_link):
        self._existing_links_processed += 1
        self._valid_links_skipped += 1

        logger.info('Skipped valid link %(link_url)s from ICE entry %(entry_uuid)s' % {
                    'link_url': experiment_link.url,
                    'entry_uuid': ice_entry.uuid,
                })

    def updated_perl_link(self, ice_entry, experiment_link):
        self._existing_links_processed += 1
        self._perl_links_updated += 1

        logger.info('Updated perl link %(link_url)s from ICE entry %(entry_uuid)s' % {
                       'link_url': experiment_link.url,
                       'entry_uuid': ice_entry.uuid, })

    def found_orphaned_edd_strain(self, strain):
        self.processed_edd_strain(strain)
        self._orphaned_edd_strains.append(strain)

    def found_stepchild_edd_strain(self, edd_strain):
        self.processed_edd_strain(edd_strain)
        self._stepchild_edd_strains.append(edd_strain)
        logger.warning("EDD strain %(strain_pk)d references an ICE entry that couldn't be found. "
                       "No ICE entry was found with uuid %(uuid)s . Skipping this strain (probably "
                       "referenced from the wrong ICE instance)." % {
                            'strain_pk': edd_strain.pk,
                            'uuid': edd_strain.registry_id, })

    def found_non_strain_entry(self, edd_strain, ice_entry):
        self.processed_edd_strain(edd_strain)
        self._non_strain_ice_parts_referenced.append(ice_entry)

        logger.warning('EDD *strain* %(edd_strain_pk)d references ICE entry "%(ice_entry_name)s", '
                       'but is defined as a %(entry_type)s. Links will not be examined for this '
                       'part, since manual curation is required. ICE entry is %(part_number)s ('
                       'uuid %(entry_uuid)s)' % {
                            'edd_strain_pk': edd_strain.pk,
                            'ice_entry_name': ice_entry.name,
                            'entry_type': ice_entry.__class__.__name__,
                            'part_number': ice_entry.part_id,
                            'entry_uuid': ice_entry.uuid, })

    def created_missing_link(self, ice_entry, study_url):
        self._missing_links_created += 1

        logger.info('Created missing link %(link_url)s from ICE entry %s(part_number)s '
                    '(%(uuid entry_uuid)s)' % {
                        'link_url': study_url,
                        'part_number': ice_entry.part_id,
                        'entry_uuid': ice_entry.uuid, })

    def processed_edd_strain(self, strain):
        self._processed_edd_strain_uuids[strain.registry_id] = True

    @property
    def existing_links_processed(self):
        return self._existing_links_processed

    @property
    def previously_processed_strains_skipped(self):
        return self._previously_processed_strains_skipped

    @previously_processed_strains_skipped.setter
    def previously_processed_strains_skipped(self, num_skipped):
        self._previously_processed_strains_skipped = num_skipped

    def print_summary(self):
        print(OUTPUT_SEPARATOR)
        print('Processing Summary')
        print(OUTPUT_SEPARATOR)

        percent_strains_processed = (self.total_edd_strains_processed /
                                     self.total_edd_strains_found) * 100 if \
                                     self.total_edd_strains_found else 0

        ############################################################################################
        # Print summary of EDD strain processing
        ############################################################################################
        print('')
        print("Note: there's potential for overlap between subsections here! ICE entries "
              "referenced by EDD strains are "
              "examined while scanning EDD strains during the first step. If configured, ICE "
              "entries not examined during the first step will also be scanned/processed "
              "independently of EDD to catch any dangling links to studies that no longer exist "
              "in EDD.")
        print('')
        subsection_header = ('EDD strains (processed/found): %(strains_found)s / '
                             '%(strains_processed)s '
                             '(%(percent_processed)0.2f%%)' % {
                                'strains_found': locale.format('%d',
                                                               self.total_edd_strains_processed,
                                                               grouping=True),
                                'strains_processed': locale.format('%d',
                                                                   self.total_edd_strains_found,
                                                                   grouping=True),
                                'percent_processed': percent_strains_processed,
        })
        subsection_separator = '-'.rjust(len(subsection_header), '-')
        print(subsection_separator)
        print(subsection_header)
        print(subsection_separator)

        follow_up_items = OrderedDict({
            'Non-strain ICE entries referenced by EDD': locale.format('%d',
                    len(self._non_strain_ice_parts_referenced), grouping=True),
            "Orphaned EDD strains (don't reference an ICE entry)": locale.format('%d',
                    len(self._orphaned_edd_strains), grouping=True),
            "Stepchild EDD strains (reference a UUID not found in this ICE deployment)":
                locale.format('%d', len(self._stepchild_edd_strains), grouping=True)

        })
        space = 3
        title_col_width = max(len(title) for title in follow_up_items.keys()) + space
        value_col_width = max(len(value) for value in follow_up_items.values()) + space

        print('\tKnown follow-up items:')

        for title, value in follow_up_items.items():
            indented_title = '\t%s ' % title.ljust(title_col_width, fill_char)
            print(fill_char.join((indented_title, value.rjust(value_col_width, fill_char))))

        ############################################################################################
        # Print summary of ICE entry processing (some is performed even if ICE isn't scanned
        # independently of EDD)
        ############################################################################################

        # account for configurability of whether ICE entries are scanned independent of their
        # relation to what's directly referenced from EDD
        entries_found = self.total_ice_entries_found if self.total_ice_entries_processed else \
                self.total_ice_entries_processed
        percent_processed = (self.total_ice_entries_processed / entries_found) * 100 if \
            entries_found else 0
        scanned_ice_entries = bool(self.total_ice_entries_found)
        scanned = 'were NOT' if not scanned_ice_entries else 'WERE'

        print('')
        subsection_header = ('ICE entries (processed/found): %(entries_processed)s / '
                             '%(entries_found)s (%(percent_processed)0.2f%%)' % {
                'entries_processed': locale.format('%d', self.total_ice_entries_processed,
                                               grouping=True),
                'entries_found': locale.format('%d', entries_found, grouping=True),
                'percent_processed': percent_processed, })
        subsection_separator = '-'.rjust(len(subsection_header), '-')

        print(subsection_separator)
        print(subsection_header)
        print(subsection_separator)

        print('\tICE entries %s scanned independently of those referenced from EDD' % scanned)
        if scanned_ice_entries:
            print('\tPreviously-processed EDD strains skipped during ICE entry scan: %s' %
                  locale.format('%d',
                    self._previously_processed_strains_skipped))

        print('')
        subsection_header = 'ICE experiment link processing:'
        subsection_separator = '-'.rjust(len(subsection_header), '-')
        print(subsection_separator)
        print(subsection_header)
        print(subsection_separator)

        first_level_summary = OrderedDict()
        first_level_summary['Missing EDD links created'] = (
            locale.format('%d', self._missing_links_created, grouping=True))
        first_level_summary['Existing links processed'] = (
            locale.format('%d', self._existing_links_processed, grouping=True))
        title_col_width = max(len(title) for title in first_level_summary.keys()) + space
        value_col_width = max(len(value) for value in first_level_summary.values()) + space
        for title, value_str in first_level_summary.items():
            indented_title = '\t%s' % title.ljust(title_col_width, fill_char)
            print(fill_char.join((indented_title, value_str.rjust(value_col_width, fill_char))))

        # build a dict of other results to be displayed so we can justify them in columns for
        # printing
        links_processed = OrderedDict()
        links_processed['Unmaintained links renamed'] = locale.format('%d',
                self._unmaintained_links_renamed, grouping=True)
        links_processed['Perl-style links updated']= locale.format('%d',self._perl_links_updated,
                                                            grouping=True)
        links_processed['Invalid links pruned']= locale.format('%d',self._invalid_links_pruned,
                                                          grouping=True)
        links_processed['Development links pruned']= locale.format('%d',
                self._development_links_pruned, grouping=True)
        links_processed['Test links pruned']= locale.format('%d', self._test_links_pruned,
                                                             grouping=True)
        links_processed['Valid links skipped']= locale.format('%d', self._valid_links_skipped,
                                                           grouping=True)
        links_processed['External links skipped']= locale.format('%d', self._skipped_external_links,
                                                        grouping=True)

        sub_title_col_width= max(len(title) for title in links_processed.keys()) + space
        sub_value_col_width = max(len(digits) for digits in links_processed.values()) + space

        # print link processing breakdown
        for title, count_str in links_processed.items():
            indented_title = '\t\t%s' % title.ljust(sub_title_col_width, fill_char)
            print(''.join((indented_title, count_str.rjust(sub_value_col_width, fill_char))))


def main():

    ############################################################################################
    # Configure command line parameters
    ############################################################################################
    parser = argparse.ArgumentParser(description='Scans EDD and ICE to locate and repair missing '
                                                 'or unmaintained links from ICE to EDD as a '
                                                 'result of temporary feature gaps in older EDD '
                                                 'versions, or as a result of communication '
                                                 'failures between EDD and ICE.')
    parser.add_argument('-p', '-password', help='provide a password via the command '
                                                'line (helps with repeated use / testing)')
    parser.add_argument('-u', '-username', help='provide username via the command line ('
                                                'helps with repeated use / testing)')
    args = parser.parse_args()

    ############################################################################################
    # Print out important parameters
    ############################################################################################
    print(OUTPUT_SEPARATOR)
    print(os.path.basename(__file__))
    print(OUTPUT_SEPARATOR)
    print('\tSettings module:\t%s' % os.environ['ICE_SETTINGS_MODULE'])
    print('\tEDD URL:\t%s' % EDD_URL)
    print('\tICE URL:\t%s' % ICE_URL)
    if args.u:
        print('\tEDD/ICE Username:\t%s' % args.u)
    print('')
    print(OUTPUT_SEPARATOR)

    overall_performance = Performance()
    processing_summary = ProcessingSummary()

    user_input = UserInputTimer()
    edd = None
    ice = None

    cleaning_edd_test_instance = is_edd_test_instance_url(EDD_URL)
    cleaning_ice_test_instance = is_ice_test_instance_url(ICE_URL)

    ############################################################################################
    # Verify that URL's start with HTTP*S* for non-local use. Don't allow mistaken config to
    # expose access credentials! Local testing requires insecure http, so this mistake is
    # easy to make!
    ############################################################################################
    if not is_url_secure(EDD_URL):
        print('EDD_BASE_URL %s is insecure. You must use HTTPS to maintain security for non-'
              'local URL\'s')
        return 0
    if not is_url_secure(ICE_URL):
        print('ICE_BASE_URL %s is insecure. You must use HTTPS to maintain security for non-'
              'local URL\'s')
        return 0

    if cleaning_edd_test_instance != cleaning_ice_test_instance:
        print("Input settings reference ICE/EDD deployments that are in different development "
              "environments (e.g. development/test/production)")
        return 0

    tested_edd_strain_count = 0
    test_edd_strain_limit = 10  # TODO: set to None following testing, but probably keep in place
    test_ice_entry_limit = 10
    hit_test_limit = False
    existing_link_processing_outcomes = {}  # entry_url ->

    try:
        ##############################
        # log into EDD
        ##############################
        login_application = 'EDD'
        edd_login_details = session_login(EddSessionAuth, EDD_URL, login_application,
                                         username_arg=args.u, password_arg=args.p,
                                         user_input=user_input, print_result=True)
        edd_session_auth = edd_login_details.session_auth

        with edd_session_auth:
            edd = EddApi(edd_session_auth, EDD_URL)
            edd.result_limit = EDD_RESULT_PAGE_SIZE

            # TODO: consider adding a REST API resource & use it to test whether this user
            # has admin access to EDD. provide an
            # early / graceful / transparent failure

            ##############################
            # log into ICE
            ##############################
            login_application = 'ICE'
            ice_login_details = session_login(IceSessionAuth, ICE_URL, login_application,
                                             username_arg=args.u, password_arg=args.p,
                                             user_input=user_input, print_result=True)
            ice_session_auth = ice_login_details.session_auth

            with ice_session_auth:
                ice = IceTestStub(ice_session_auth, ICE_URL, result_limit=ICE_RESULT_PAGE_SIZE)
                # ice.request_generator.timeout = 20

                # test whether this user is an ICE administrator. If not, we won't be able
                # to proceed until SYNBIO-TODO is resolved (if then, depending on the solution)
                user_is_ice_admin = is_ice_admin_user(ice=ice,
                                                      username=ice_login_details.username)
                if not user_is_ice_admin:
                    return 0

                ############################################################
                # Search EDD for strains, checking each one against ICE
                ############################################################
                print('')

                print(OUTPUT_SEPARATOR)
                print('Comparing all EDD strains to ICE... ')
                print(OUTPUT_SEPARATOR)

                # loop over EDD strains, processing a page of strains at a time
                edd_strain_page = edd.search_strains()
                edd_page_num = 1
                while edd_strain_page and edd_strain_page.current_result_count:

                    print('EDD: received %(received)s of %(total)s strains (page %(page_num)s)' % {
                        'received': locale.format('%d', edd_strain_page.current_result_count,
                                                  grouping=True),
                        'total': locale.format('%d', edd_strain_page.total_result_count,
                                               grouping=True),
                        'page_num': locale.format('%d', edd_page_num, grouping=True),
                    })

                    if edd_page_num == 1:
                        processing_summary.total_edd_strains_found = edd_strain_page.total_result_count

                    # loop over strains in this results page, updating ICE's links to each one
                    for edd_strain in edd_strain_page.results:
                        processed = process_edd_strain(edd_strain, edd, ice,
                                                       processing_summary, overall_performance,
                                                       cleaning_ice_test_instance)


                        # enforce a small number of tested strains for starters so tests complete
                        # quickly
                        tested_edd_strain_count += 1
                        hit_test_limit = tested_edd_strain_count == test_edd_strain_limit
                        if hit_test_limit or is_aborted():
                            print('Hit test limit of %d EDD strains. Ending strain processing '
                                  'early.' % test_edd_strain_limit)
                            break

                    if hit_test_limit or is_aborted():
                        break

                    # get another page of strains from EDD
                    if edd_strain_page.is_paged() and edd_strain_page.next_page:
                        edd_strain_page = edd.search_strains(query_url=edd_strain_page.next_page)
                        edd_page_num += 1
                    else:
                        edd_strain_page = None

            if processing_summary.orphaned_edd_strain_count:
                logger.warning('Skipped %d EDD strains that were incompletely had no UUID' %
                               processing_summary.orphaned_edd_strain_count)

            overall_performance.completed_edd_strain_scan()
            print('Done processing EDD strains in %s' % to_human_relevant_delta(
                    overall_performance.edd_strain_scan_time.total_seconds()))


            # if configured, process entries in ICE that weren't just examined above.
            # This is probably only necessary during the initial run to correct for link maintenance
            # gaps in earlier versions of EDD/ICE
            if PROCESS_ICE_ENTRIES:
                overall_performance.ice_entry_scan_start_time = arrow.utcnow()
                process_ice_entries(ice, edd, search_ice_part_types, processing_summary,
                                    cleaning_ice_test_instance, test_ice_entry_limit)
                overall_performance.ice_entry_scan_time = arrow.utcnow() - overall_performance.ice_entry_scan_start_time

    except Exception as e:
        logger.exception('An error occurred')
    finally:
        print('')
        processing_summary.print_summary()

        overall_performance.overall_end_time = arrow.utcnow()
        if edd:
            overall_performance.edd_communication_time = edd.request_generator.wait_time
        if ice:
            overall_performance.ice_communication_time = ice.request_generator.wait_time

        print('')
        overall_performance.print_summary()


def process_ice_entries(ice, edd, search_ice_part_types,
                        processing_summary,
                        cleaning_ice_test_instance, test_ice_entry_limit):
    """
    Searches ICE for entries of the specified type(s), then examines experiment links for each part
    whose ID isn't in processed_edd_strain_uuids, comparing any EDD-referencing experiment links to
    EDD and maintaining the links as appropriate.
    :param ice: an authenticated IceApi instance
    :param edd: an authenticated EddApi instance
    :param search_ice_part_types: a list of entry types to be examined in ICE, or None to examine
    all entries.
    :param processed_edd_strain_uuids: a dictionary whose keys are the UUIDs of ICE strains already
    examined from just-compeleted processing of strains from EDD. All ICE entries whose ID's are
    contained here will be skipped on the assumption that their associated links have just finished
    being maintained, and with high probability are still up-to-date.
    :param cleaning_ice_test_instance: true if the ICE instance being maintained is a test instance.
    If False, all reverences to EDD test instances will be removed on the assumption that they
    were accidental artifacts of software testing with improperly configured URLs.
    """
    print('')
    print(OUTPUT_SEPARATOR)
    print('Comparing ICE entries to EDD... ')
    print(OUTPUT_SEPARATOR)

    ice_entry_search_results_page = ice.search_entries(entry_types=search_ice_part_types)
    ice_page_num = 1
    tested_ice_entry_count = 0
    hit_test_limit = False

    # loop over ICE entries, finding and pruning stale links to EDD strains / studies
    # that no longer reference them. We'll skip ICE entries that we just examined from
    # the EDD perspective above, since there's a low probability they've been updated since
    while ice_entry_search_results_page and ice_entry_search_results_page.current_result_count:
        print('ICE: received %(received)s of %(total)s entries (page %(page_num)s)' % {
            'received': locale.format('%d', ice_entry_search_results_page.current_result_count,
                                      grouping=True),
            'total': locale.format('%d', ice_entry_search_results_page.total_result_count,
                                   grouping=True),
            'page_num': locale.format('%d', ice_page_num, grouping=True)})

        if ice_page_num == 1:
            processing_summary.total_ice_entries_found = ice_entry_search_results_page.total_result_count

        # loop over ICE entries in the current results page
        for ice_entry_search_result in ice_entry_search_results_page.results:
            entry = ice_entry_search_result.entry

            # skip entries that we just processed when examining EDD strains. Possible these
            # relationships have changed since our pass through EDD, but most likely that
            # nothing has changed or that EDD properly maintained the ICE links in the interim
            if processing_summary.is_edd_strain_processed(entry.uuid):
                processing_summary.previously_processed_strains_skipped += 1
                continue

            process_ice_entry(ice, edd, entry, processing_summary, cleaning_ice_test_instance)
            tested_ice_entry_count += 1

            hit_test_limit = tested_ice_entry_count == test_ice_entry_limit

            if hit_test_limit:
                print("Hit test limit of %d ICE entries. Ending ICE entry processing early." %
                      tested_ice_entry_count)
                break

        if hit_test_limit:
            break

        # if available, get another page of results
        if ice_entry_search_results_page.is_paged() and ice_entry_search_results_page.next_page:
            ice_entry_search_results_page = ice.search_entries(entry_types=search_ice_part_types,
                                                               page_number=ice_page_num)
            ice_page_num += 1

    if not ice_entry_search_results_page:
        logger.warning("Didn't find any ICE parts in the search")


def process_ice_entry(ice, edd, entry, processing_summary, cleaning_ice_test_instance):
    """
    Processes a single ICE entry, checking its experiment links and creating / maintaining any
    included links to EDD. The base assumption of this method is that EDD has been recently scanned
    for strains, and that all of the associated ICE entries have recently been updated. This method
    should only be called to test ICE entries that weren't updated as part of that process (and so
    barring modifications in the interim, are unlikely to have any current/valid links to EDD).
    """

    start_time = arrow.utcnow()

    # build a (short-lived) list of experiment links for this entry.
    # Note: possible, but unlikely we're causing race conditions by caching this data
    entry_experiments_list = build_ice_entry_experiments_cache(ice, entry.uuid)

    # examine experiment links for this entry, removing or updating those that are
    # no longer valid, or that were unambiguously created as a result of software
    # testing
    for experiment_link in entry_experiments_list:
        outcome = do_initial_run_ice_entry_link_processing(ice, edd, entry, experiment_link,
                                                           cleaning_ice_test_instance,
                                                           True, processing_summary)
        # if we've already processed the link, move to the next one
        processed_link = outcome != NOT_PROCESSED_OUTCOME
        if processed_link:
            continue

        # don't modify any experiment URL that doesn't directly map to
        # a known EDD URL. Researchers can create these automatically, and we
        # don't want to remove any that EDD didn't create
        study_url_match = STUDY_URL_PATTERN.match(experiment_link.url)
        if not study_url_match:
            processing_summary.skipped_external_link(entry, experiment_link)
            continue

        study_pk = int(study_url_match.group('study_id'))

        # since we didn't process this entry when examining links from the EDD
        # perspective, check in with EDD again in case this entry was updated
        # following our previous check
        try:
            edd_study_strains = edd.get_study_strains(study_pk)

            if not edd_study_strains:
                processing_summary.removed_invalid_link(entry, experiment_link)
                ice.remove_experiment_link(entry.uuid, experiment_link.id)
            else:
                processing_summary.skipped_valid_link(entry, experiment_link)

        except HTTPError as err:
            if err.response.status_code == requests.codes.not_found:
                logger.warning("Ice entry %d has an experiment link to EDD study "
                               "%d, but the EDD study wasn't found. Removed a link referencing "
                               "this study")
                ice.remove_experiment_link(entry.uuid, experiment_link.id)
                processing_summary.removed_invalid_link(entry, experiment_link)
            else:
                raise err

    processing_summary.total_ice_entries_processed += 1
    run_duration = arrow.utcnow() - start_time
    print('Processed %(link_count)d entry experiment links in %(runtime)s for ICE entry '
          '%(part_id)s (uuid %(part_uuid)s)' % {
            'part_id': entry.part_id,
            'part_uuid': entry.uuid,
            'link_count': len(entry_experiments_list),
            'runtime': to_human_relevant_delta(run_duration.total_seconds()), })



def do_initial_run_ice_entry_link_processing(ice, edd, ice_entry, experiment_link,
                                             cleaning_ice_test_instance, query_edd_strains,
                                             processing_summary):
    """
    Processes an ICE experiment link and performs updates that are specific to the initial
    successful run of this script, based on the known development & integration history of EDD
    and ICE at JBEI.
    :param query_edd_strains: True to query EDD for matching strains if the link is detected to be
    an older Perl-style link to EDD. If False, and if this is a Perl-style link, it will be removed
    from ICE without further checking. If True, we'll confirm that the link is valid with EDD
    before removing it.
    :return: True if version-specific processing was completed for this experiment link, indicating
    that  further processing isn't needed
    """
    # remove link if it's to a development machine
    if is_development_url(experiment_link.url):
        ice.remove_experiment_link(ice_entry.uuid, experiment_link.id)
        processing_summary.removed_development_link(ice_entry, experiment_link)
        return REMOVED_DEVELOPMENT_URL_OUTCOME

    # remove link if it's from a production ICE instance to a test EDD instance
    if (not cleaning_ice_test_instance) and is_edd_test_instance_url(experiment_link.url):
        ice.remove_experiment_link(ice_entry.uuid, experiment_link.id)
        processing_summary.removed_test_link(ice_entry,experiment_link)
        return REMOVED_TEST_URL_OUTCOME

    # update link if it references an outdated EDD URL scheme.
    # redirects will work, but old-style URLs will be missed by this scripts / EDD's consistency
    # checks during link maintenance
    perl_study_url_match = PERL_STUDY_URL_PATTERN.match(experiment_link.url)
    if not perl_study_url_match:
        return NOT_PROCESSED_OUTCOME

    study_pk = int(perl_study_url_match.group('study_id'))
    study_strains = edd.get_study_strains(study_pk) if query_edd_strains else None
    if not study_strains:
        ice.remove_experiment_link(ice_entry.uuid, experiment_link.id)
        processing_summary.removed_invalid_link(ice_entry, experiment_link)
        return REMOVED_BAD_STUDY_LINK

    # update the Perl-era link to match the new link format
    study_url = edd.get_abs_study_browser_url(study_pk)
    study = edd.get_study(study_pk)

    ice.link_entry_to_study(ice_entry.uuid, study_pk, study_url, study.name,
                            old_study_url=experiment_link.url, logger=logger)
    processing_summary.updated_perl_link(ice_entry, experiment_link)
    return UPDATED_PERL_URL_OUTCOME


def build_ice_entry_experiments_cache(ice, entry_uuid):
    # TODO: consolidate comments here
    """
    Queries ICE and creates a cache of experiment links for this ICE part. To reduce the chances of
    encountering a race condition during ongoing user modifications to EDD / ICE, the lifetime of
    this cache data should be minimized. Thankfully, if a race condition is encountered and EDD / ICE
    experiments get out-of-sync, we should be able to re-run this script to correct problems
    (and with low probability, create some new ones).

     NOTE: for absolute consistency with EDD, we'd have to temporarily disable or delay
     creations/edits for lines so we don't create a race condition for
     updates to this strain that occur while the script is inspecting this
     it. Instead, we'll opt for simplicity and tolerate a small chance of
     creating new inconsistencies with the script, since we can just run it
     again to correct errors / potentially create new ones :-

     NOTE: To
     reduce the risk of race conditions creating consistency issues
     in ICE, we could just push updates to all of the strains referenced by
     EDD, but instead we'll accept increased risk of race conditions by
     temporarily creating and referencing a local cache of ICE's
     experiment links for this strain to help us
     avoid unnecessary overhead from messaging (which is much more likely to
      be a problem at present)

      :returns a map of lower-case link url -> ExperimentLink for all links associated with this
      entry
    """
    all_experiment_links = {}

    results_page = ice.get_entry_experiments(entry_uuid)
    while results_page and not is_aborted():
        for link in results_page.results:
            all_experiment_links[link.url.lower()] = link

        # get another page of results
        if results_page.next_page:
            results_page = ice.get_entry_experiments(
                    query_url=results_page.next_page)
        else:
            results_page = None

    return all_experiment_links


def process_edd_strain(edd_strain, edd, ice, processing_summary, overall_performance,
                       cleaning_ice_test_instance):
        """
        Processes a single EDD strain, verifying that ICE already has links to its associated
        studies,
        or creating / maintaining them as needed to bring ICE up-to-date.
        :param edd_strain: the edd Strain to process
        :param edd: an EddApi instance
        :param ice: an IceApi instance
        :param incomplete_edd_strains, a list to add this strain to if it processing is skipped
        because
        it doesn't have the required ICE URL / UUID
        :param overall_performance: a Performance object for tracking time spent on various tasks
        during the (long) execution time of the whole program
        :return true if the strain was successfully processed, false if something prevented it from
        being processed
        """

        if not edd_strain.registry_id:
            processing_summary.found_orphaned_edd_strain(edd_strain)
            return False

        # TODO: make this a decorator
        strain_performance = StrainProcessingPerformance(arrow.utcnow(),
                                                         edd.request_generator.wait_time,
                                                         ice.request_generator.wait_time)
        # get a
        # reference to the ICE part referenced from this EDD strain. because
        # of some initial gaps in EDD's strain creation process, it's possible that
        # a few non-strains snuck in here that we need to detect.
        # Additionally, looking up the ICE part gives us a cleaner way of working
        #  around SYNBIO-XXX, which causes ICE to return 500 error instead of 404
        # when experiments can't be found for a non-existent part

        ice_entry = ice.get_entry(edd_strain.registry_id)
        if not ice_entry:
            processing_summary.found_stepchild_edd_strain(edd_strain)
            return False

        if not isinstance(ice_entry, IceStrain):
            processing_summary.found_non_strain_entry(edd_strain, ice_entry)
            return False

        ice_entry_uuid = edd_strain.registry_id
        all_strain_experiment_links = build_ice_entry_experiments_cache(ice, ice_entry_uuid)
        unprocessed_strain_experiment_links = all_strain_experiment_links.copy()

        strain_performance.ice_link_search_time = (arrow.utcnow() - strain_performance.start_time)
        missing_strain_study_links = all_strain_experiment_links.copy()

        # query EDD for all studies that reference this strain
        strain_studies_page = edd.get_strain_studies(edd_strain.pk) if not is_aborted() else None
        while strain_studies_page and not is_aborted():

            for study in strain_studies_page.results:
                # if is_aborted(): # TODO: consider re-adding if we can't use Celery's
                # AbortableTask,
                #  and therefore don't have to worry about the performance hit for testing aborted
                #  status
                #     break

                study_url = edd.get_abs_study_browser_url(study.pk).lower()
                strain_performance.ice_link_search_time = None
                strain_to_study_link = all_strain_experiment_links.get(study_url)
                unprocessed_strain_experiment_links.pop(study_url)

                if strain_to_study_link:
                    missing_strain_study_links.pop(study_url)
                else:
                    # look for an unmaintained link to the study URL from the older
                    # perl version of EDD (these exist!). If found, update it.
                    perl_study_url = build_perl_study_url(study.pk).lower()
                    strain_to_study_link = missing_strain_study_links.pop(perl_study_url)
                    if not strain_to_study_link:
                        perl_study_url = build_perl_study_url(study.pk, https=True).lower()
                        strain_to_study_link = missing_strain_study_links.pop(perl_study_url)

                    if strain_to_study_link:
                        ice.link_entry_to_study(ice_entry_uuid, study.pk, study_url,
                                                study.name, old_study_url=perl_study_url,
                                                logger=logger)
                        processing_summary.updated_perl_link(ice_entry, strain_to_study_link)
                        continue

                if (not strain_to_study_link) or (strain_to_study_link.label != study.name):
                    old_study_name = strain_to_study_link.label if strain_to_study_link else None
                    ice.link_entry_to_study(ice_entry_uuid, study.pk, study_url, study.name,
                                            logger, old_study_name)
                    if old_study_name:
                        processing_summary.renamed_unmaintained_link(ice_entry,
                                                                     strain_to_study_link,
                                                                     study.name)
                    else:
                        processing_summary.created_missing_link(ice_entry, study_url)
                    strain_performance.links_updated += 1
                else:
                    strain_performance.links_skipped += 1
                    overall_performance.total_links_already_valid += 1
                    processing_summary.skipped_valid_link(ice_entry, strain_to_study_link)

            if strain_studies_page.next_page:
                strain_studies_page = edd.get_strain_studies(
                    query_url=strain_studies_page.next_page)
            else:
                strain_studies_page = None

        # look over ICE experiment links for this entry that we didn't add, update, or remove as a
        # result of up-to-date study/strain associations in EDD. If any remain that match the
        # pattern of EDD URL's, they're outdated and need to be removed. This complete processing
        # of the ICE entry's experiment links will also allow us to skip over this entry later
        # in the process if we scan ICE to look for other entries with outdated links to EDD
        for link_url, experiment_link in unprocessed_strain_experiment_links.iteritems():
            outcome = do_initial_run_ice_entry_link_processing(ice, edd, ice_entry,
                                                               experiment_link,
                                                     cleaning_ice_test_instance,
                                                     False, processing_summary)
            if outcome == NOT_PROCESSED_OUTCOME:
                # don't modify any experiment URL that doesn't directly map to
                # a known EDD URL. Researchers can create these automatically, and we
                # don't want to remove any that EDD didn't create
                study_url_match = STUDY_URL_PATTERN.match(experiment_link.url)
                if study_url_match:
                    ice.remove_experiment_link(ice_entry_uuid, experiment_link.id)
                    processing_summary.removed_invalid_link(ice_entry_uuid, experiment_link)
                else:
                    processing_summary.skipped_external_link(ice_entry, experiment_link)

        # track performance for completed processing
        strain_performance.ice_link_cache_lifetime = arrow.utcnow() - strain_performance.start_time
        strain_performance.set_end_time(arrow.utcnow(), edd.request_generator.wait_time,
                                        ice.request_generator.wait_time)
        strain_performance.print_summary()

        processing_summary.processed_edd_strain(edd_strain)
        return True


def verify_ice_admin_privileges(ice, ice_username):
    ice_admin_user = is_ice_admin_user(ice, ice_username)
    if ice_admin_user is None:
        print('Unable to determine whether user "%s" has administrative privileges on'
              'ICE. Administrative privileges are required to update links to strains '
              'users don\'t have direct write access to.' % ice_username)
        print('Aborting the link maintenance process.')
        return False
    if not ice_admin_user:
        print('User "%s" doesn\'t have administrative privileges on ICE. S\he won\'t be'
              'able to update links for strains s\he doesn\'t have write access to.' %
              ice_username)
        print('Aborting the link maintenance process.')
        return False
    return True


if __name__ == '__main__' or __name__ == 'jbei.edd.rest.scripts.maintain_ice_links':
    result = main()
    exit(result)


