# -*- coding: utf-8 -*-
from __future__ import division
from __future__ import unicode_literals

from jbei.rest.api import RestApiClient

"""
Defines classes and utility methods used to communicate with the Index of Composable Elements
(ICE), a.k.a. the "registry of parts". This API is designed to minimize dependencies on other
libraries (e.g. Django model objects) so that it can be used from any part of the EDD codebase,
including remotely-executed code, with a minimum of network traffic and install process. For
example, many of the methods in the IceApi class are called from Celery tasks that may execute on
a physically separate server from EDD itself, where Django model objects shouldn't be passed over
the network.
"""


import base64
import hashlib
import hmac
import importlib

from urllib import urlencode
from urlparse import urlunparse, ParseResult, parse_qs

from jbei.rest.utils import remove_trailing_slash, CLIENT_ERROR_NOT_FOUND
from jbei.rest.request_generators import (RequestGenerator, SessionRequestGenerator, PagedResult,
                                          PagedRequestGenerator)
import json
import logging
import os
import re
import requests
from requests.auth import AuthBase
from requests.compat import urlparse

logger = logging.getLogger(__name__)

####################################################################################################
# Set reasonable defaults where possible
####################################################################################################
ICE_REQUEST_TIMEOUT = (10, 10)  # request and read timeout, respectively, in seconds
ICE_URL = 'https://registry.jbei.org'
ICE_SECRET_KEY = None

####################################################################################################
# Perform flexible configuration based on whether or not client code or an environment
# variable has defined an alternate source of the required settings. If not, define some defaults
# here that will be used instead (though notably, no HMAC key will be available).
####################################################################################################

# if an ICE-specific settings module has been defined, override defaults with values provided there.
settings_module_name = os.environ.get('ICE_SETTINGS_MODULE')
if settings_module_name:
    settings = importlib.import_module(settings_module_name)
    if hasattr(settings, 'ICE_REQUEST_TIMEOUT'):
        ICE_REQUEST_TIMEOUT = settings.ICE_REQUEST_TIMEOUT
    if hasattr(settings, 'ICE_URL'):
        ICE_URL = settings.ICE_URL
    if hasattr(settings, 'ICE_SECRET_HMAC_KEY'):
        ICE_SECRET_KEY = settings.ICE_SECRET_KEY

# otherwise, if an a django settings module is defined, get configuration from there instead
else:
    settings_module_name = os.environ.get('DJANGO_SETTINGS_MODULE')
    if settings_module_name:
        # override defaults with values provided by Django settings. This dependency on
        # Django should be kept as contained as possible to prevent non-Django clients from
        # having to load a LOT of unnecessary libraries.
        try:
            import django.conf
            django_settings = django.conf.settings
            if hasattr(django_settings, 'ICE_REQUEST_TIMEOUT'):
                ICE_REQUEST_TIMEOUT = django_settings.ICE_REQUEST_TIMEOUT
            if hasattr(django_settings, 'ICE_URL'):
                ICE_URL = django_settings.ICE_URL
            if hasattr(django_settings, 'ICE_SECRET_HMAC_KEY'):
                ICE_SECRET_KEY = django_settings.ICE_SECRET_HMAC_KEY
        except ImportError as i:
            logger.error('DJANGO_SETTINGS_MODULE environment variable was provided as a source of '
                         'settings, but an import error occurred while trying to load Django '
                         'settings.')
            raise i


####################################################################################################

_JSON_CONTENT_TYPE_HEADER = {'Content-Type': 'application/json; charset=utf8'}

# regular expressions for parsing elements of ICE URLs
_PROTOCOL = 'http|https'
_BASE_ICE_URL_REGEX = r'.+'
# TODO: better to check for format of UUID. it's: 8 chars -4 chars -4 chars -4 chars -12 chars
_IDENTIFIER = r'[\w-]+'
_ICE_ENTRY_URL_REGEX = '(%(protocol)s)://(%(base_url)s)/entry/(%(identifier)s)/?' % {
    'protocol': _PROTOCOL, 'base_url': _BASE_ICE_URL_REGEX, 'identifier': _IDENTIFIER, }
ICE_ENTRY_URL_PATTERN = re.compile('^%s$' % _ICE_ENTRY_URL_REGEX, re.IGNORECASE)


####################################################################################################
# String constants used to communicate with ICE.
# TODO: most/all of these should be enums after upgrading to Python 3
####################################################################################################
DEFAULT_RESULT_LIMIT = 15  # ICE's current automatic limit on results returned in the absence of a
                           # specific requested page size
DEFAULT_PAGE_NUMBER = 1

STRAIN = 'STRAIN'
PLASMID = 'PLASMID'
PART = 'PART'
ARABIDOPSIS = 'ARABIDOPSIS'
ICE_ENTRY_TYPES = (STRAIN,
                   PLASMID,
                   ARABIDOPSIS,)

ARABIDOPSIS_DATA_JSON_KEYWORD = 'Arabidopsis'

BLAST_N = 'BLAST_N'
TBLAST_X = 'TBLAST_X'
BLAST_PROGRAMS = (BLAST_N, TBLAST_X)
RESULT_LIMIT_PARAMETER = 'limit'
RESULT_OFFSET_PARAMETER = 'offset'

HOST_PYTHON_PARAM = 'host'
HOST_JSON_PARAM = 'host'
GENOTYPE_PHENOTYPE_PYTHON_PARAM = 'genotype_phenotype'
STRAIN_DATA_JSON_KEYWORD = 'strainData'
PLASMID_DATA_JSON_KEYWORD = 'plasmidData'
####################################################################################################

PART_ID_JSON_FIELD = 'partId'
PART_KEYWORD_CHANGES = {
    'accessPermissions': 'access_permissions',
    'basePairCount': 'bp_count',
    'bioSafetyLevel': 'biosafety_level',
    'canEdit': 'can_edit',
    'creationTime': 'creation_time',
    'creatorEmail': 'creator_email',
    'creatorId': 'creator_id',
    'featureCount': 'feature_count',
    'fundingSource': 'funding_source',
    'hasAttachment': 'has_attachment',
    'hasOriginalSequence': 'has_original_sequence',
    'hasSample': 'has_sample',
    'hasSequence': 'has_sequence',
    'intellectualProperty': 'intellectual_property',
    'longDescription': 'long_description',
    'modificationTime': 'mod_time',
    'ownerEmail': 'owner_email',
    'ownerId': 'owner_id',
    PART_ID_JSON_FIELD: 'part_id',
    'principalInvestigator': 'pi_name',
    'principalInvestigatorEmail': 'pi_email',
    'principalInvestigatorId': 'pi_id',
    'publicRead': 'public_read',
    'recordId': 'uuid',
    'selectionMarkers': 'selection_markers',
    'shortDescription': 'short_description',
    'viewCount': 'view_count',
}

GENOTYPE_PHENOTYPE_JSON_PARAM = 'genotypePhenotype'
STRAIN_KEYWORD_CHANGES = {
    GENOTYPE_PHENOTYPE_JSON_PARAM: GENOTYPE_PHENOTYPE_PYTHON_PARAM
}

PLASMID_KEYWORD_CHANGES = {
    'originOfReplication': 'origin_of_replication',
    'replicatesIn': 'replicates_in'
}

ARABIDOPSIS_KEYWORD_CHANGES = {
    'harvestDate': 'harvest_date',
    'seedParents': 'seed_parents',
    'plantType': 'plant_type',
    'sentToAbrc': 'sent_to_a_brc',
}

class Entry(object):
    """
    The Python representation of an ICE entry. Note that in ICE, Part has only one unique field
    that's deprecated.
    """

    def __init__(self, id=None, visible=None, parents=[], index=None, uuid=None,
                 name=None, owner=None, owner_email=None, owner_id=None, creator=None,
                 creator_email=None, creator_id=None, status=None, short_description=None,
                 long_description=None,
                 creation_time=None, mod_time=None, biosafety_level=None, part_id=None, links=[],
                 pi_name=None, pi_email=None, pi_id=None, selection_markers=None, bp_count=None,
                 feature_count=None, view_count=None, has_attachment=None, has_sample=None,
                 has_sequence=None, has_original_sequence=None, can_edit=None,
                 access_permissions=[], public_read=False, linked_parts=[], alias=None, keywords=[],
                 intellectual_property=None, references=None, funding_source=None):
        self.id = id
        self.visible = visible
        self.parents = []
        self.index = index
        self.uuid = uuid
        self.name = name
        self.owner = owner
        self.owner_email = owner_email
        self.owner_id = owner_id
        self.creator = creator
        self.creator_email = creator_email
        self.creator_id = creator_id
        self.status = status
        self.short_description = short_description
        self.long_description = long_description
        self.creation_time = creation_time
        self.mod_time = mod_time
        self.biosafety_level = biosafety_level
        self.part_id = part_id
        self.links = links
        self.pi_name = pi_name
        self.pi_email = pi_email
        self.pi_id = pi_id
        self.selection_markers = selection_markers
        self.bp_count = bp_count
        self.feature_count = feature_count
        self.view_count = view_count
        self.has_attachment = has_attachment
        self.has_sample = has_sample
        self.has_sequence = has_sequence
        self.has_original_sequence = has_original_sequence
        self.can_edit = can_edit
        self.access_permissions = access_permissions
        self.public_read = public_read
        self.linked_parts = linked_parts
        self.alias = alias
        self.keywords = keywords
        self.intellectual_property = intellectual_property
        self.references = references
        self.funding_source = funding_source
        self.parents = parents

    @staticmethod
    def of(json_dict, silence_type_specific_warnings):
        """
        Factory method for creating a Part from a JSON dictionary received from ICE.
        :param json_dict: a dictionary representation of the ICE JSON for this part
        :return: an object representing the part, or None if there's none in the input
        """
        # convert to a dictionary of native Python types

        if not json_dict:
            return None

        # build up a list of keyword arguments to use in constructing the Part.
        python_object_params = {}

        ############################################################################################
        # Convert data we have a custom Python object for, where the trivial JSON conversion
        # won't cover it.
        #  TODO: untested / no known examples of these...may be wrong. If just strings, no need for
        # parsing here
        ############################################################################################
        parameters = json_dict.get('parameters', [])

        # linked parts
        LINKED_PARTS_JSON_KEYWORD = 'linkedParts'
        linked_parts_temp = json_dict.get(LINKED_PARTS_JSON_KEYWORD)
        linked_parts = (Entry.of(linked_part_dict) for linked_part_dict in linked_parts_temp) if \
                       linked_parts_temp else []
        python_object_params['linked_parts'] = linked_parts

        # parents
        PARENTS_JSON_KEYWORD = 'parents'
        PARENTS_PYTHON_KEYWORD = 'parents'
        parents_list = json_dict.get(PARENTS_JSON_KEYWORD)
        parents_list = (Entry.of(parent_dict) for parent_dict in parents_list) if parents_list \
            else []
        python_object_params[PARENTS_PYTHON_KEYWORD] = parents_list

        # set/replace object parameters in the dictionary
        LINKED_PARTS_PYTHON_KEYWORD = 'linked_parts'

        nontrivial_conversion_keywords = {
            LINKED_PARTS_JSON_KEYWORD: LINKED_PARTS_PYTHON_KEYWORD,
            PARENTS_JSON_KEYWORD: PARENTS_PYTHON_KEYWORD,
        }
        ############################################################################################
        # set objects that have a trivial conversion from JSON to Python,
        # changing the style to match Python's snake_case from the ICE's Java-based camelCase
        ############################################################################################

        # TODO: can do this with less code by automatically converting camel case for most of
        # these. At least one known solution exists at
        # http://stackoverflow.com/questions/1175208/elegant-python-function-to
        #  -convert-camelcase-to-camel-case, but seems a bit problematic, license-wise.


        # TODO: investigate JSON data in this dictionary that we don't currently understand /
        # support.
        IGNORED_PART_KEYWORDS = [
            'parameters'
        ]

        for json_keyword, json_value in json_dict.iteritems():
            # skip data that translate to custom Python objects rather than builtin data types
            if json_keyword in nontrivial_conversion_keywords:
                continue

            if json_keyword in IGNORED_PART_KEYWORDS:
                continue

            python_keyword = PART_KEYWORD_CHANGES.get(json_keyword, json_keyword)
            python_object_params[python_keyword] = json_value

        part_type = python_object_params.pop('type')  # Note: don't shadow Python builtin 'type'!

        if PLASMID == part_type:
            return _construct_part(python_object_params, part_type, PLASMID_DATA_JSON_KEYWORD,
                                   PLASMID_KEYWORD_CHANGES, Plasmid, silence_type_specific_warnings)

        if STRAIN == part_type:
            return _construct_part(python_object_params, part_type, STRAIN_DATA_JSON_KEYWORD,
                             STRAIN_KEYWORD_CHANGES, Strain, silence_type_specific_warnings)

        if ARABIDOPSIS == part_type:
            return _construct_part(python_object_params, part_type, ARABIDOPSIS_DATA_JSON_KEYWORD,
                                   ARABIDOPSIS_KEYWORD_CHANGES, Arabidopsis,
                                   silence_type_specific_warnings)

        if PART == part_type:
            return Entry(**python_object_params)

        raise Exception('Unsupported type "%s"' % part_type)

    def __str__(self):
        return '%s / "%s"' % (self.part_id, self.name)

    def to_json_dict(self):
        # copy all data members into a dictionary
        json_dict = self.__dict__.copy()

        # reverse the json -> python keyword changes performed during deserialization
        for json_keyword, python_keyword in PART_KEYWORD_CHANGES.iteritems():
            value = json_dict.pop(python_keyword)
            if value:
                json_dict[json_keyword] = value

        return json_dict


class ExperimentLink(object):
    """
    The Python implementation of an 'experiment link' stored by ICE to reference
    another arbitrary URL
    """
    def __init__(self, id, url, owner_email, creation_time, label=None):
        self.label = label
        self.id = id
        self.url = url
        self.owner_email = owner_email
        self.creation_time = creation_time

    @staticmethod
    def of(json_dict):
        return ExperimentLink(
                json_dict['id'],
                json_dict['url'],
                json_dict.get('ownerEmail'),
                json_dict['created'],
                label=json_dict.get('label'),)

####################################################################################################
# ICE Sample Location Types. See org.jbei.ice.lib.dto.sample.SampleType
####################################################################################################
PLATE96 ='PLATE96'
PLATE81 = 'PLATE81'
WELL = 'WELL'
TUBE = 'TUBE'
SCHEME = 'SCHEME'
ADDGENE ='ADDGENE'
GENERIC = 'GENERIC'
FREEZER = 'FREEZER'
SHELF = 'SHELF'
BOX_INDEXED = 'BOX_INDEXED'
BOX_UNINDEXED = 'BOX_UNINDEXED'

LOCATION_TYPES = (PLATE96, PLATE81, GENERIC, FREEZER, SHELF, BOX_INDEXED, BOX_UNINDEXED, PLATE81,
                  WELL, TUBE, SCHEME, )


class Location(object):
    """
    The Python representation of a sample storage location
    """
    def __init__(self, id, display, location_type, name, child=None):
        self.id = id
        self.display = display
        self.location_type = location_type
        self.child = child
        self.name = name

    @staticmethod
    def of(json_dict):
        child_dict = json_dict.get('child')
        child = Location.of(child_dict) if child_dict else None

        loc_type = json_dict['type']
        if loc_type not in LOCATION_TYPES:
            raise ValueError('Location type "%s" is not supported.')

        return Location(
            id=json_dict['id'],
            display=json_dict['display'],
            location_type=json_dict['type'],
            name=json_dict['name'],
            child=child,
        )

    def is_plate(self):
        return self.location_type in (PLATE81, PLATE96)

    def is_well(self):
        return self.location_type == WELL

    def can_contain_plates(self):
        return self.location_type in (GENERIC, FREEZER, SHELF, BOX_INDEXED, BOX_UNINDEXED)


class Sample(object):
    """
    The Python representation of a biological sample
    """
    def __init__(self, id, depositor, label, location, creation_time, part_id, can_edit,
                 comments, in_cart=False):
        self.id = id
        self.depositor = depositor
        self.label = label
        self.location = location
        self.part_id = part_id
        self.can_edit = can_edit
        self.comments = comments
        self.creation_time = creation_time
        self.in_cart = in_cart

    @staticmethod
    def of(json_dict):

        # translate from camel-case JSON keywords to snake case Python data member names
        json_to_python_keyword_changes = {
            'partId': 'part_id',
            'canEdit': 'can_edit',
            'creationTime': 'creation_time',
            'inCart': 'in_cart',
        }

        translated_dict = {}
        for java_keyword, value in json_dict.items():
            python_keyword = json_to_python_keyword_changes.get(java_keyword, java_keyword)
            translated_dict[python_keyword] = value

        # unpack User object
        depositor_dict = translated_dict.pop('depositor')
        depositor = User.of(depositor_dict)

        # unpack Location object
        location_dict = translated_dict.pop('location')
        location = Location.of(location_dict)

        return Sample(depositor=depositor, location=location, **translated_dict)


def _construct_part(python_object_params, part_type, class_data_keyword, conversion_dict,
                    part_derived_class, silence_type_specific_warnings):
    # extract strain-specific data, if available. change camel case to snake case.

    class_data = python_object_params.pop(class_data_keyword, None)
    if class_data:
        for keyword, value in class_data.items():
            python_keyword = keyword
            if keyword in conversion_dict:
                python_keyword = conversion_dict[keyword]
            python_object_params[python_keyword] = value
    elif not silence_type_specific_warnings:
        logger.warning('JSON for %(class_name)s "%(part_id)s" has type=%(type)s, but no '
                       '%(field_name)s field.' % {
                            'class_name': part_derived_class.__name__,
                            'part_id': python_object_params['part_id'],
                            'type': part_type,
                            'field_name': class_data_keyword, })
    return part_derived_class(**python_object_params)


def _convert_json_keywords(json_dict, conversion_dict):
    """
    Makes a shallow copy of a dictionary with JSON-formatted, producing a dictionary with
    Python-formatted keys
    :param json_dict: the JSON dictionary
    :param conversion_dict: a dictionary that maps JSON keywords to their Python equivalents. Any keywords
    not present here are assumed to be identical in both.
    :return: a new dictionary with Python-formatted keys
    """
    converted_dict = {}
    for keyword, value in json_dict:
        python_keyword = conversion_dict[keyword]
        if not python_keyword:
            python_keyword = keyword
        converted_dict[python_keyword] = value
    return converted_dict

NORMAL_ACCOUNT_TYPE = 'NORMAL'
ADMIN_ACCOUNT_TYPE = 'ADMIN'
USER_ACCOUNT_TYPES = (NORMAL_ACCOUNT_TYPE, ADMIN_ACCOUNT_TYPE)

class User(object):
    def __init__(self, id, email, initials, first_name, last_name, institution, description,
                 last_login, registration_date, user_entry_count, visible_entry_count, is_admin,
                 new_message_count, account_type, default_permissions_list):
        self.id = id
        self.email = email
        self.initials = initials
        self.first_name = first_name
        self.last_name = last_name
        self.institution = institution
        self.description = description
        self.last_login = last_login
        self.registration_date = registration_date
        self.user_entry_count = user_entry_count
        self.visible_entry_count = visible_entry_count
        self.is_admin = is_admin
        self.new_message_count = new_message_count
        self.account_type = account_type
        self.default_permissions_list = default_permissions_list # TODO: check possible values

    @staticmethod
    def of(json_dict):
        json_to_python_keyword_changes = {
            'firstName': 'first_name',
            'lastName': 'last_name',
            'lastLogin': 'last_login',
            'registerDate': 'registration_date',
            'userEntryCount': 'user_entry_count',
            'visibleEntryCount': 'visible_entry_count',
            'isAdmin': 'is_admin',
            'newMessageCount': 'new_message_count',
            'accountType': 'account_type',
            'defaultPermissions': 'default_permissions_list',
        }

        translated_dict = {}
        for java_keyword, value in json_dict.items():
            python_keyword = json_to_python_keyword_changes.get(java_keyword, java_keyword)
            translated_dict[python_keyword] = value

        return User(**translated_dict)


class EntrySearchResult(object):
    # TODO: resolve nident changes with Hector P -- not pushed to Github yet, though recently
    # observed on registry-test
    def __init__(self,  entry, e_value, query_length, score, max_score, match_details, nident=None,):
        self.entry = entry
        self.e_value = e_value
        self.query_length = query_length
        self.nident = nident
        self.score = score
        self.max_score = max_score
        self.match_details = match_details

    @staticmethod
    def of(json_dict):
        # build a dict of keywords for translating field names from Java-based conventions used in
        # ICE's JSON to Python style names
        python_entry_keyword = 'entry'
        keyword_dict = {
            'eValue': 'e_value',
            'queryLength': 'query_length',
            'maxScore': 'max_score',
            'matchDetails': 'match_details',
            'entryInfo': 'entry',
        }

        translated_dict = {}
        for java_keyword, value in json_dict.iteritems():
            python_keyword = keyword_dict.get(java_keyword, java_keyword)

            # read the part into a Part object
            if python_keyword == python_entry_keyword:
                # NOTE: ICE doesn't return type-specific data as part of its search results
                value = Entry.of(value, silence_type_specific_warnings=True)

            translated_dict[python_keyword] = value

        return EntrySearchResult(**translated_dict)


class Strain(Entry):
    def __init__(self, host=None, genotype_phenotype=None, **kwargs):
        super(Strain, self).__init__(**kwargs)
        self.host = host
        self.genotype_phenotype = genotype_phenotype

    def to_json_dict(self):
        json_dict = super(Strain, self).to_json_dict()

        # remove strain-specific data from the dictionary and re-package it as in ICE's JSON
        host_value = json_dict.pop(HOST_PYTHON_PARAM)
        geno_value = json_dict.pop(GENOTYPE_PHENOTYPE_PYTHON_PARAM)

        strain_data = {}
        if host_value:
            strain_data[HOST_JSON_PARAM] = host_value
        if geno_value:
            strain_data[GENOTYPE_PHENOTYPE_JSON_PARAM]

        if strain_data:
            json_dict[STRAIN_DATA_JSON_KEYWORD] = strain_data

        return json_dict


### Design note: all part-specific params are currently optional so that we can still at least
# capture the part type when the part gets returned from a search without any of its type-specific
# data. TODO: confirm with Hector P. that this is intentional, then make them non-optional if needed
class Plasmid(Entry):
    def __init__(self, backbone=None, origin_of_replication=None, promoters=None, circular=None,
                 replicates_in=None, **kwargs):
        super(Plasmid, self).__init__(**kwargs)

        self.backbone = backbone
        self.origin_of_replication = origin_of_replication
        self.promoters = promoters
        self.circular = circular
        self.replicates_in = replicates_in


# TODO: class is a draft / isn't tested
class Arabidopsis(Entry):
    def __init__(self, ecotype=None, harvest_date=None, seed_parents=None, generation=None,
                 plant_type=None, sent_to_a_brc=None, **kwargs):
        super(Arabidopsis, self).__init__(**kwargs)
        self.ecotype = ecotype
        self.harvest_date = harvest_date
        self.seed_parents = seed_parents
        self.generation = generation
        self.plant_type = plant_type
        self.sent_to_a_brc = sent_to_a_brc


def parse_entry_id(ice_entry_url):
    """
    Extracts identifier for the ICE part that is identified by the URL parameter. Depending on
    how the URL is defined, this may be either the locally-unique part ID used within a given ICE
    deplopment, or the UUID that provides a global unique ID for the part.
    :param ice_entry_url:
    the fully-qualified URL of the ICE part.
    :return: the identifier for the part within its host
    ICE deployment, or None if the input didn't match the expected pattern.
    """
    match = ICE_ENTRY_URL_PATTERN.match(ice_entry_url)

    if not match:
        return None

    return match.group(3)

DEFAULT_HMAC_KEY_OWNER = 'edd'


class HmacAuth(AuthBase):
    """
    Implements Hash-based Message Authentication Codes (HMAC). HMAC guarantees that: A) a message
    has been generated by a holder of the secret key, and B) that its contents haven't been
    altered since the auth code was generated.
    Instances of HmacAuth are immutable and are therefore safe to use in multiple threads.
    :param username the username of the ice user who ICE activity will be attributed to.
    Overrides the value provided by user_auth if both are present. At least one is required.
    :raises ValueError if no user email address is provided.
    """
    # TODO: remove remaining ICE-specific code/variable names to make this code more generic,
    # then relocate. May need to create an ICE-specific subclass.

    def __init__(self, request_generator, secret_key, key_owner=None, username=None, ):
        """

        :param secret_key:
        :param key_owner:
        :param username:
        :param request_generator: the request generator to use
        :return:
        """
        if not secret_key:
            raise ValueError("A secret key is required input for HMAC authentication")
        self._KEY_OWNER = key_owner
        self._USERNAME = username
        self._SECRET_KEY = secret_key

        if not request_generator:
            request_generator = RequestGenerator(auth=self)
        else:
            request_generator.auth = self
        self._request_generator = request_generator

    @property
    def request_generator(self):
        """
        Get the request generator responsible for creating all requests to the remote server.
        """
        return self._request_generator

    def __call__(self, request):
        """
        Overrides the empty base implementation to provide authentication for the provided request
        object.
        """

        # generate a signature for the message by hashing the request using the secret key
        sig = self._build_signature(request)

        # add message headers including the username (if present) and message
        # TODO: maybe this is ICE-specific?
        header = ':'.join(filter(bool, ('1', self._KEY_OWNER, self._USERNAME, sig)))
        request.headers['Authorization'] = header
        return request

    def _build_message(self, request):
        """
        Builds a string representation of the message contained in the request so it can be digested
        for HMAC generation
        """
        url = urlparse(request.url)

        # build up the message, using only components that evaluate to True
        delimiter = '\n'
        msg = delimiter.join(filter(bool, (self._USERNAME,
                             request.method,
                             url.netloc,
                             url.path,
                             self._sort_parameters(url.query),
                             )))

        # append msg body even if empty
        msg = delimiter.join((msg, request.body or ''))
        return msg

    def _build_signature(self, request):
        """
        Builds a signature for the provided request message based on the secret key.
        """
        key = base64.b64decode(self._SECRET_KEY)
        msg = self._build_message(request)
        digest = hmac.new(key, msg=msg, digestmod=hashlib.sha1).digest()
        sig = base64.b64encode(digest).decode()
        return sig

    def _sort_parameters(self, query):
        params = sorted(map(lambda p: p.split('=', 1), query.split('&')), key=lambda p: p[0])
        return '&'.join(map(lambda p: '='.join(p), params))

    ############################################
    # 'with' context manager implementation ###
    ############################################
    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        pass
    ############################################


class IceHmacAuth(HmacAuth):
    def __init__(self, secret_key, key_owner=DEFAULT_HMAC_KEY_OWNER, username=None,
                 request_generator=None):
        """

        :param secret_key:
        :param key_owner:
        :param username:
        :param request_generator: the RequestGenerator instance to use, or None to create a new
        PagedRequestGenerator.
        :return:
        """
        self.__KEY_OWNER = key_owner
        self.__USER_EMAIL = username

        if not request_generator:
            request_generator = PagedRequestGenerator(RESULT_LIMIT_PARAMETER,
                                                      RESULT_OFFSET_PARAMETER, auth=self)
        else:
            request_generator.auth = self

        super(IceHmacAuth, self).__init__(request_generator, secret_key)

    @staticmethod
    def get(secret_key=ICE_SECRET_KEY, username=None, user_auth=None, request=None):
        """
        Factory method for creating an HMAC authentication instance for communicating with ICE using
        a combination of HMAC and the user ID of an *authenticated* EDD user. All actions
        performed in ICE using the resulting authentication instance
        will be attributed to the user with this ID, but trusted by ICE since they're signed by
        the secret key. It's crucial for security communications using this mechanism only be
        exposed to authenticated users or trusted systems.

        At least one of the last three parameters must be provided as a source of ICE username.
        :param secret_key: the secret key used to sign messages as the basis of HMAC authentication.
        if no key is provided, an attempt is made to read it from django's settings.
        :param username: the username of the ICE user to which subsequent ICE activity will
        be attributed
        :param user_auth: the user auth for an EDD user, which is assumed to contain a
        :param request: the authenticated EDD request to get user information from
        :return: a new HmacAuth instance
        """

        if username:
            return IceHmacAuth(secret_key=secret_key, username=username)
        elif user_auth and user_auth.email:
            return IceHmacAuth(secret_key=secret_key, username=user_auth.email)
        elif request and request.user:
            return IceHmacAuth(secret_key=secret_key, username=request.user)
        else:
            raise ValueError("At least one source of ICE username for an authenticated EDD user is "
                             "required")


class SessionAuth(AuthBase):
    """
    Implements session-based authentication for ICE. At the time of initial implementation,
    "session-based" is a bit misleading for the processing performed here, since ICE's login
    mechanism doesn't reply with set-cookie headers or read the session ID in the session cookie.
    Instead, ICE's REST API responds to a successful login with a JSON object containing the session
    ID, and authenticates subsequent requests by requiring the session ID in each subsequent
    request header.

    Clients should first call login() to get a valid ice session id
    """
    def __init__(self, session_id, session, timeout=ICE_REQUEST_TIMEOUT, verify_ssl_cert=True):
        self._session_id = session_id
        self._session = session

        session_request_generator = SessionRequestGenerator(session, auth=self, timeout=timeout,
                                                          verify_ssl_cert=verify_ssl_cert)

        paging_request_generator = PagedRequestGenerator(request_api=session_request_generator,
                                                         result_limit_param_name=RESULT_LIMIT_PARAMETER ,
                                                         result_limit=DEFAULT_RESULT_LIMIT)

        self._request_generator = paging_request_generator

    @property
    def request_generator(self):
        """
        Get the request generator responsible for creating all requests to the remote server.
        """
        return self._request_generator

    def __call__(self, request):
        """
        Overrides the empty base implementation to provide authentication for the provided request
        object (which should normally be _session). ICE doesn't seem to read the session ID from
        cookies, so there's no specific need to provide those here.
        """
        request.headers['X-ICE-Authentication-SessionId'] = self._session_id
        return request

    ############################################
    # 'with' context manager implementation ###
    ############################################
    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self._session.__exit__(type, value, traceback)
    ############################################

    @staticmethod
    def login(password, username=None, user_auth=None, base_url=ICE_URL,
              timeout=ICE_REQUEST_TIMEOUT, verify_ssl_cert=True):

        """
        Logs into ICE at the provided base URL or raises an Exception if an unexpected response is
        received from the server.
        :param base_url: the base URL of the ICE installation (not including the protocol
        :param timeout a tuple representing the connection and read timeouts, respectively, in
        seconds, for the login request to ICE's REST API
        :param verify_ssl_cert True to verify ICE's SSL certificate. Provided so clients can ignore
        self-signed certificates during *local* EDD / ICE testing on a single development machine.
        Note that it's very dangerous to skip certificate verification when communicating across
        the network, and this should NEVER be done in production.
        :return: new SessionAuth containing the newly-created session. Note that at present the
        session isn't strictly required, but is provided for completeness in case ICE's
        behavior changes to store the session ID as a cookie instead of requiring it as a request
        header.
        """

        if not username:
            username = user_auth.email if user_auth else None

        if not username:
            raise ValueError("At least one source of ICE username is required")

        # chop off the trailing '/', if any, so we can write easier-to-read URL snippets in our code
        # (starting w '%s/'). also makes our code trailing-slash agnostic.
        base_url = remove_trailing_slash(base_url)

        # begin a session to track any persistent state required by the server
        session = requests.session()

        # build request parameters for login
        login_dict = {'email': username,
                      'password': password}
        login_resource_url = '%(base_url)s/rest/accesstokens/' % {'base_url': base_url}

        # issue a POST to request login from the ICE REST API
        response = session.post(login_resource_url, headers=_JSON_CONTENT_TYPE_HEADER,
                                data=json.dumps(login_dict), timeout=timeout,
                                verify=verify_ssl_cert)

        # raise an exception if the server didn't give the expected response
        if response.status_code != requests.codes.ok:
            response.raise_for_status()

        json_response = response.json()
        session_id = json_response['sessionId']

        # if login failed for any other reason,
        if not session_id:
            raise ValueError("Server responded successfully, but response did not include the "
                             "required session id")

        logger.info('Successfully logged into ICE at %s' % base_url)

        return SessionAuth(session_id, session)


class IceApi(RestApiClient):
    """
    Defines the interface to ICE's REST API.

    TODO: extremely basic interface to ICE API; should eventually expand to cover more
    of the API, modularize (i.e. so others can just import jbei.ice), and document.
    """
    # TODO: prioritize returning objects here over JSON, which is should be available to
    # front-end code, but better encapsulated for client-side REST API access. EDD should expose
    # JSON data as a view in its rest API rather than here.
    # TODO: when returning model objects, prevent database changes via partially-populated model
    # object instances. See draft code in edd.py

    def __init__(self, auth, base_url=ICE_URL, result_limit=DEFAULT_RESULT_LIMIT):
        """
        Creates a new instance of IceApi
        :param auth: the authentication strategy for communication with ICE
        :param base_url: the base URL of the ICE install.
        :param result_limit: the maximum number of results that can be returned from a single
        query. The default is ICE's default limit at the time of writing. Note that ICE
        doesn't return paging-related data from its REST API, so to
        provide consistent tracking of how results are paged, some value has to be provided.
        """
        if not auth:
            raise ValueError("A valid authentication mechanism must be provided")

        super(IceApi, self).__init__('ICE', base_url, auth.request_generator,
                                             result_limit)

    def _compute_result_offset(self, page_number):
        result_limit = self.result_limit

        if page_number:
            if not result_limit:
                raise ValueError("Non-unity page number specified, but result offset can't be "
                                 "computed because the result_limit isn't known")
            else:
                return result_limit * (page_number - 1)

        return None

    def _add_page_number_param(self, dict, page_number):
        if page_number:
            if not self.result_limit:
                if page_number != 1:
                    logger.warning("A non-unity page number was requested, but can't be honored "
                                   "because no result_limit is known!")
            else:
                offset = self.request_generator.result_limit * (page_number - 1)
                dict[RESULT_OFFSET_PARAMETER] = offset

    def _extract_pagination_params(self, query_url):
        query_params_string = urlparse(query_url).query
        query_dict = parse_qs(query_params_string) if query_params_string else None

        offset = query_dict.get[RESULT_OFFSET_PARAMETER]
        if offset:
            offset = offset[0]

    def search_users(self, search_string=None, sort=None, asc=None, page_number=DEFAULT_PAGE_NUMBER,
                     query_url=None, ):
        """
        Searches for users known to this instance of ICE, if the authenticated user has the
        appropriate access privileges in ICE.
        :param filter:
        :param sort:
        :param asc:
        :param page_number: the page number of results to be returned (1-indexed)
        :return:
        :raise HttpError: if the authenticated user doesn't have access to this ICE
        resource (isn't a sysadmin), or if some other error has occurred.
        """
        self._verify_page_number(page_number)
        # TODO: investigate / hard-code / check for supported values of 'sort' param

        # construct a dictionary of query params in the format ICE expects

        response = None
        if query_url:
            response = self.request_generator.get(query_url)
        else:
            url = '%s/rest/users' % self.base_url

            query_params = {}

            if search_string:
                query_params['filter'] = search_string

            if sort:
                query_params['sort'] = sort

            if asc:
                query_params['asc'] = sort

            self._add_page_number_param(query_params, page_number)

            response = self.request_generator.get(url, params=query_params)

        if response.status_code == requests.codes.ok:
            return IcePagedResult.of(response.content, User, results_key='users',
                                     query_url=response.url)

        response.raise_for_status()

    def get_entry_experiments(self, entry_id, query_url=None, page_number=DEFAULT_PAGE_NUMBER):
        """
        Retrieves ICE's experiments links for the specified entry, using any of the unique
        identifiers: part id, synthetic id, or UUID.
        :param entry_id: the ICE ID for this entry
        :param page_number: the page number of results to be returned (1-indexed)
        :return: A PagedResult containing at least one EntryLink object, or None if the ICE
        returned an empty (but successful) response.
        """

        self._verify_page_number(page_number)

        response = None
        if query_url:
            response = self.request_generator.get(query_url, headers=_JSON_CONTENT_TYPE_HEADER)

        else:
            query_params = {}
            self._add_page_number_param(query_params, page_number)

            # execute the query
            url = '%s/rest/parts/%s/experiments/' % (self.base_url, entry_id)

            response = self.request_generator.get(url,
                params=query_params,
                headers=_JSON_CONTENT_TYPE_HEADER
            )

            query_url = response.url

        if response.status_code == requests.codes.ok:
            return IcePagedResult.of(response.content, ExperimentLink, query_url=query_url)
        else:
            # NOTE: we purposefully DON'T return None for 404, since that would remove the clients'
            # ability to distinguish between a non-existent entry and an entry with no experiments
            response.raise_for_status()


    def get_entry_samples(self, entry_id, query_url=None, page_number=DEFAULT_PAGE_NUMBER):
        """
        Retrieves ICE's samples for the specified entry, using any of the unique
        identifiers: part id, local integer primary key, or UUID.
        :param entry_id: the ICE ID for this entry
        :param page_number: the page number of results to be returned (1-indexed)
        :return: A PagedResult containing at least one Sample, or None if ICE
        returned an empty (but successful) response.
        """
        self._verify_page_number(page_number)

        response = None
        if query_url:
            response = self.request_generator.get(query_url, headers=_JSON_CONTENT_TYPE_HEADER)

        else:
            query_params = {}
            self._add_page_number_param(query_params, page_number)

            # execute the query
            url = '%s/rest/parts/%s/samples/' % (self.base_url, entry_id)
            response = self.request_generator.get(url, params=query_params,
                                                  headers=_JSON_CONTENT_TYPE_HEADER)
            query_url = response.url

        if response.status_code == requests.codes.ok:
            return IcePagedResult.of(response.content, Sample, query_url=query_url)
        else:
            # NOTE: we purposefully DON'T return None for 404, since that would remove the clients'
            # ability to distinguish between a non-existent entry and and entry with no samples
            response.raise_for_status()

    def get_entry(self, entry_id, suppress_errors=False):
        """
        Retrieves a part using any of the unique identifiers: part number, synthetic id, or
        UUID. Returns a Part object; or None if no part was found or if there were suppressed
        errors in making the request.
        :param entry_id: the ICE ID for this entry (either the local numeric primary key, or a UUID)
        :param suppress_errors: true to catch and log exception messages and return None instead of
        raising Exceptions.
        :return: A Part object representing the response from ICE, or None if an an Exception
        occurred but suppress_errors was true.
        """

        rest_url = '%s/rest/parts/%s' % (self.base_url, entry_id)
        try:
            response = self.request_generator.get(url=rest_url)
        except requests.exceptions.Timeout as e:

            if not suppress_errors:
                raise e
            logger.exception("Timeout requesting part %s: %s", entry_id)
        else:
            if response.status_code == requests.codes.ok:
                # convert reply to a dictionary of native python data types
                json_dict = json.loads(response.content)

                if not json_dict:
                    return None

                return Entry.of(json_dict, False)

            elif response.status_code == CLIENT_ERROR_NOT_FOUND:
                return None

            if not suppress_errors:
                response.raise_for_status()

            logger.exception('Error fetching part from ICE with entry_id %(entry_id)s. '
                             'Response = %(status_code)d: "%(msg)s"'
                             % {'entry_id': entry_id,
                                'status_code': response.status_code,
                                'msg': response.reason
                                })

            return None

    # TODO: doesn't support field filters yet, though ICE's API does
    def search_entries(self, search_terms=None, entry_types=None, blast_program=None,
                       blast_sequence=None, search_web=False,
                       sort_field=None, sort_ascending=False, page_number=DEFAULT_PAGE_NUMBER,
                       suppress_errors=False):
        # TODO: consider removing suppress_errors and forcing client code to support that function,
        # when/if needed
        """
        Calls ICE's REST API to search for a biological part using the provided query string
        :param search_terms: a string with keyword search terms. If the string is
        None, all ICE parts visible to the user will be returned.
        :param entry_types: a list of entry types to be included in the results. Must be one of
        ICE_ENTRY_TYPES.
        :param blast_program: the program to use in comparing blast_sequence to the sequence of
        the parts in ICE
        :param blast_sequence: the sequence to compare against parts. Either base pairs or amino
        acids, depending on the value of blast_program.
        :param search_web: True to search the web of registries, false to search just this one
        :param offset: the offset into the full query results from which the returned data should
        start
        :param sort_ascending: true to sort in ascending order, False otherwise. Ignored if
        sort_field is None
        :param sort_ascending: True to sort in ascending order, False for descending order
        :param page_number: the page number of results to be returned (1-indexed)
        :param suppress_errors: True to suppress errors
        :return: a single page of results. Note that this method is a special case since the full
        functionality of ICE's search only seems to be supported by POST, so unlike many other
        results, the PagedResult.next_page link won't work to load a subsequent page of results,
        though it can be used to detect whether more results exist
        :raises KeyError: if entry_types is included, but contains something other than one of
        the valid Ice entry types, or if blast_program is included, but isn't one of the
        recognized programs.
        """

        self._verify_page_number(page_number)
        query_url = None  # TODO: re-instate this parameter if we can get ICE to support the same
        # queries in GET as in POST...should simplify client use by allowing

        logger.info('Searching for ICE entries using search terms "%s"' % search_terms)

        url = '%s/rest/search' % self.base_url

        offset = None

        # package up provided parameters (if any) for insertion into the request
        # optional_query_data = json.dumps({'queryString': query}) if query else None
        query_dict = {}
        if not query_url:
            if search_terms:
                query_dict['q'] = search_terms
            if entry_types:
                if not set(entry_types).issubset(set(ICE_ENTRY_TYPES)):
                    raise KeyError('')
                query_dict['entryTypes'] = entry_types
            if blast_program:
                if blast_program not in BLAST_PROGRAMS:
                    raise KeyError('Blast program %s is not one of the recognized programs: ' %
                                   (blast_program, str(BLAST_PROGRAMS)))
                if blast_sequence:
                    query_dict['blastQuery'] = {
                        'blastProgram': blast_program,
                        'blastSequence': blast_sequence,
                    }
                else:
                    logger.warning('A blast program was specified, but no blast sequence. Ignoring '
                                   'the program.')
            elif blast_sequence:
                logger.warning('A blast sequence was specified, but no blast program. Ignoring the '
                               'sequence.')
            if search_web:
                query_dict['webSearch'] = search_web


            ########################################################################################
            # Build a list of query parameters that get bundled together in a slightly non-standard
            # way
            ########################################################################################
            parameters = {}
            if sort_field:
                parameters['sortField'] = sort_field
            if sort_ascending:
                parameters['asc'] = sort_ascending

            nonstandard_offset_param = 'start'
            nonstandard_result_limit_param = 'retrieveCount'
            # override processing normally handled my request_generator

            # apply non-standard page numbering / result limiting needed by this ICE resource
            if page_number:
                if not self.result_limit:
                    if page_number != 1:
                        logger.warning("A non-unity page number was requested, but can't be honored "
                                       "because no result_limit is known!")
                else:
                    offset = self.request_generator.result_limit * (page_number - 1)
                    parameters[nonstandard_offset_param] = offset

            if self.result_limit:
                parameters[nonstandard_result_limit_param] = self.result_limit

            if parameters:
                query_dict['parameters'] = parameters

        else:
            # un-parse the query URL so we're using consistently following the same code path
            query_dict = parse_qs(urlparse(query_url).params)

        # convert query data to JSON, if there is any. Otherwise, we'll query for all the parts
        # visible to this user
        optional_query_data = json.dumps(query_dict) if query_dict else None

        # execute the query
        try:
            response = self.request_generator.post(url,
                data=optional_query_data,
                headers=_JSON_CONTENT_TYPE_HEADER
            )
            # if response was good, deconstruct the query url, then build a separate 'get' URL
            # to use in
            if response.status_code == requests.codes.ok:

                if not query_url:
                    url_elts = urlparse(url)
                    query_string = urlencode(query_dict, True)
                    query_temp = ParseResult(url_elts.scheme, url_elts.netloc,
                                                   url_elts.path, url_elts.params,
                                                   query_string, url_elts.fragment)
                    query_url = urlunparse(query_temp)

                return IcePagedResult.of(response.content, EntrySearchResult,
                                         query_url=query_url,
                                         result_limit=self.result_limit,
                                         offset=offset)
            elif suppress_errors:
                logger.exception('Error searching ICE entries using query "%(query_str)s". '
                                 'Response was %(status_code)s: "%(msg)s"' %
                                 {
                                     'query_str': search_terms,
                                     'status_code': response.status_code,
                                     'msg': response.reason
                                 })
                return None
            else:
                response.raise_for_status()
        except requests.exceptions.Timeout as e:
            if not suppress_errors:
                raise e
            logger.exception('Timeout searching ICE for query "%s"' % search_terms)

    def _create_or_update_link(self, study_name, study_url, entry_experiments_url,
                               link_id=None):
        """
            A helper method that creates or updates a single study link in ICE. Note that ICE seems
            to do some URL-based matching / link replacement even if no link ID is provided.
            :raises requests.exceptions.Timeout if the initial connection or response times out
        """
        # NOTE: this implementation works, but can probably be simplified based on how ICE actually
        # behaves vs. what the original plan was. Probably best to wait for comments and see
        # whether SYNBIO-1196 changes (see associated
        # comments). Currently, there's no need to provide the link ID at all when adding/updating.

        json_dict = {'label': study_name, 'url': study_url}
        json_str = json.dumps(json_dict)

        if logger:
            logger.info(
                "Requesting part-> study link from ICE (id=%s): %s" %
                (str(link_id), entry_experiments_url)
            )
            logger.info("Response: %s " % json_str)

        headers = {'Content-Type': 'application/json'}
        if link_id:
            headers['id'] = link_id

        request_generator = self.request_generator
        response = request_generator.request('POST', entry_experiments_url,
                                             data=json_str,
                                             headers=headers)

        if response.status_code != requests.codes.ok:
            response.raise_for_status()

    def unlink_entry_from_study(self, ice_entry_id, study_id, study_url, logger):
        """
        Contacts ICE to find and remove all the links from the specified ICE part to the
        specified EDD study. In practical use, there will probably only ever be one per
        part/study combination.
        :param ice_entry_id: the id of the ICE entry whose link to the study should be removed (
        either a UUID or the numeric id)
        :param study_id: the study ID to display in log messages (though the study may have been
        deleted).
        :param study_url: the study URL. Link removal is based on case-insensitive matching against
        this value
        :param logger: the logger to log messages to
        :return true if a link to the specified study was removed from ICE, false if no such link
        existed (but no error occurred)
        :raises HTTPError if a communication error occurred or if the server responded with a
        status code other than 200
        :raises requests.exceptions.Timeout if a communication timeout occurs.
        """
        logger.info('Start ' + self.unlink_entry_from_study.__name__ + "()")
        self._prevent_write_while_disabled(self)

        # Look up the links associated with this ICE part
        entry_experiments_rest_url = self._build_entry_experiments_url(ice_entry_id)

        response = self.request_generator.request('GET', entry_experiments_rest_url,
                                             headers=_JSON_CONTENT_TYPE_HEADER)
        if response.status_code != requests.codes.ok:
            response.raise_for_status()

        # Filter out links that aren't for this study
        json_dict = response.json()  # TODO: doesn't account for
        study_links = [link for link in json_dict if study_url.lower() == link.get('url').lower()]
        logger.debug("Existing links response: " + json_dict.__str__())

        if not study_links:
            logger.warning('No existing links found for (entry %s, study %d). Nothing to remove!'
                           % (ice_entry_id, study_id))
            return False

        # Delete all links that reference this study URL
        for link in study_links:
            link_id = link.get('id')
            logger.info('Deleting link %d from entry %s' % (link_id, ice_entry_id))
            self.remove_experiment_link(ice_entry_id, link_id)

        return True

    def _build_entry_experiments_url(self, ice_entry_id):
        return '%s/rest/parts/%s/experiments/' % (self.base_url, ice_entry_id)

    def remove_experiment_link(self, ice_entry_id, link_id):
        """
        Removes the specified experiment link from an ICE entry
        """
        self._prevent_write_while_disabled(self)

        entry_experiments_rest_url = self._build_entry_experiments_url(ice_entry_id)
        link_resource_uri = entry_experiments_rest_url + "%s/" % link_id
        response = self.request_generator.request('DELETE', link_resource_uri)

        if response.status_code != requests.codes.ok:
            response.raise_for_status()

    def link_entry_to_study(self, ice_entry_id, study_id, study_url, study_name, logger,
                            old_study_name=None, old_study_url=None):
        """
        Communicates with ICE to link an ICE entry to an EDD study, or if a link to this URL
        already exists, updates the labels for the all the existing ICE experiment links that
        uses this URL (even for entries other than the one specified by ice_entry_id). See
        comments on SYNBIO-1196.
        Note that because of the way ICE's REST API responds, this implementation performs multiple
        round-trips to  ICE to check whether the link exists before creating it. A future
        improvement is to fully characterize / unit test the ICE API's behavior, then to provide
        a more efficient low-level alternative to support clients that have already performed
        their own checking.
        :param ice_entry_id: the string used to identify the strain ( either the string
        representation of the number displayed in the URL, or the UUID stored in EDD's database)
        :param study_id: the unique ID of this study
        :param study_url: the URL for the EDD study to link to the ICE strain. Case-insensitive
        matching of this URL against any existing links in ICE determines whether an existing
        link is updated or whether a new link is created.
        :param study_name: the name of the EDD study, which will be used to label the link
        created in ICE
        :param old_study_name: the previous name of the EDD study (assumption is that it was just
        renamed). If provided, all ICE links with this name and the study_url will be updated to
        use the new name, unless it exactly matches study_name, in which case it's ignored.
        :param old_study_url: the previous URL of the EDD study (assumption is that the EDD study
        URL has changed). If provided, all ICE links referencing this URL (case insensitive) will be
        updated to use the new URL, unless it exactly matches study_url, in which case it's ignored.
        :raises HTTPError if a communication error occurred or if the server responded with a status
        code other than 200
        :raises requests.exceptions.Timeout if a communication timeout occurs.
        """
        logger.info('Start ' + self.link_entry_to_study.__name__ + '()')
        self._prevent_write_while_disabled()

        # NOTE: this implementation works, but can probably be simplified based on how ICE actually
        # behaves vs. what the original plan was. Probably best to wait for comments and see
        # whether SYNBIO-1196 changes (see associated comments). Currently, there's no need to
        # account for possibility of multiple ICE links from a single entry to the same EDD
        # study, since ICE won't support multiple links to the same URL (the latest just
        # overwrites).

        # query ICE to get the list of existing links for this part
        entry_experiments_rest_url = self._build_entry_experiments_url(ice_entry_id)
        logger.info(entry_experiments_rest_url)
        response = self.request_generator.request('GET', entry_experiments_rest_url,
                                                  headers=_JSON_CONTENT_TYPE_HEADER)
        if response.status_code != requests.codes.ok:
            response.raise_for_status()

        # inspect results to find the unique ID's for any pre-existing links referencing the study's
        # URL
        label_key = 'label'
        url_key = 'url'
        existing_links = response.json()  # TODO: doesn't account for results paging see EDD-200
        current_study_links = [link for link in existing_links if
                               ((study_url.lower() == link.get(url_key).lower()) and
                                (study_name == link.get(label_key)))]
        outdated_study_links = []
        old_study_name = old_study_name if old_study_name and old_study_name != study_name else None
        old_study_url = old_study_url if old_study_url and old_study_url != study_url else None
        if old_study_name or old_study_url:
            outdated_study_links = [link for link in existing_links if
                                    (old_study_name and ((study_url.lower() == link.get(
                                            url_key).lower()) and
                                            (old_study_name == link.get(label_key)))) or
                                    (old_study_url and old_study_url.lower() == link.get(
                                            url_key).lower())]

        logger.debug('Existing links: ' + str(existing_links))
        logger.debug('Current study links:' + str(current_study_links))
        logger.debug('Outdated study links: ' + str(outdated_study_links))

        # if there's at least one up-to-date link to the study, and there are no outdated links to
        # it, just return without making any changes
        if current_study_links and not outdated_study_links:
            return

        # create or update study links
        if outdated_study_links:
            for outdated_link in outdated_study_links:
                self._create_or_update_link(study_name, study_url, entry_experiments_rest_url,
                                            link_id=outdated_link.get('id'))
        else:
            self._create_or_update_link(study_name, study_url, entry_experiments_rest_url)


class IcePagedResult(PagedResult):
    # TODO: think more about whether / how to propagate JSON query parameters as part of POST
    # requests
    @staticmethod
    def of(json_string, factory_class, query_url, results_key=u'results', result_limit=None,
           offset=0):
        """
        Reads a JSON string into a PagedResult containing Python objects.
        :param json_string: the result string to read / deserialize
        :param query_url: the complete URL for this query. Used to construct the prev_page/
        next_page links that should help simplify client code and make IceApi respond similarly
        to EddApi despite having different JSON paging support
        :param results_key: the JSON keyword used to differentiate results from the rest of the
        content (e.g. a total result count)
        :return:
        """
        # TODO: merge with EddPagedResult.of() if serialization problems there can be resolved

        # convert reply to a dictionary of native python data types
        json_dict = json.loads(json_string)

        if not json_dict:
            return None

        # pull out the 'results' subsection *if* the data is paged
        response_content = None
        count = None
        next_page = None
        prev_page = None

        # if response is paged, infer paging context to provide a consistent client interface
        # with PagedResults returned by EDD. ICE doesn't include next/previous explicitly in EDD's
        # results.
        if results_key in json_dict:
            response_content = json_dict.get(results_key)
            count = json_dict.get(u'resultCount', None)
            # for consistency, construct next/prev page URLs for ICE results that are automatically
            if query_url:

                # extract elements of the query URL so we can reconstruct it using paging parameters
                url_elts = urlparse(query_url)
                query_params_dict = parse_qs(url_elts.query)

                # if paging parameters aren't already defined, try extracting them from query_url
                if not result_limit:
                    result_limit = query_params_dict.get(RESULT_LIMIT_PARAMETER)
                    result_limit = int(result_limit[0]) if result_limit else None
                if offset is None:
                    offset_temp = query_params_dict.get(RESULT_OFFSET_PARAMETER)
                    offset_temp = int(offset_temp[0]) if offset_temp else None
                    offset = offset if offset is not None else offset_temp

                # if required inputs are available, attempt to compute next/prev URLs similar
                # to those included in EDD's JSON so we can provide a standard client-side interface
                # for both REST API's, despite the differing implementations
                if result_limit:
                    # compute page indexes for the current page and prev/next pages
                    current_page_index = (offset // result_limit)
                    next_page_index = current_page_index + 1
                    next_page_index = next_page_index if ((next_page_index * result_limit) +
                                                         result_limit <= count) or (
                        current_page_index * result_limit < count) else None
                    prev_page_index = current_page_index - 1 if current_page_index >= 1 else None

                    query_string = urlencode(query_params_dict, True) if query_params_dict else None

                    # construct next/prev URL's by deconstructing the URL for the current
                    # query, then reconstructing it using the next/prev page indices
                    # computed above
                    if next_page_index:
                        query_params_dict[RESULT_LIMIT_PARAMETER] = result_limit
                        query_params_dict[RESULT_OFFSET_PARAMETER] = next_page_index * result_limit
                        query_string = urlencode(query_params_dict, True)
                        next_page_inputs = ParseResult(url_elts.scheme, url_elts.netloc,
                                                       url_elts.path, url_elts.params,
                                                       query_string, url_elts.fragment)
                        next_page = urlunparse(next_page_inputs)

                    if prev_page_index:
                        query_params_dict[RESULT_LIMIT_PARAMETER] = result_limit
                        query_params_dict[RESULT_OFFSET_PARAMETER] = prev_page_index * result_limit
                        prev_page_inputs = ParseResult(url_elts.scheme, url_elts.netloc, url_elts.path,
                                            url_elts.params, query_string, url_elts.fragment)
                        prev_page = urlunparse(prev_page_inputs)

            if count == 0:
                return None

        # otherwise just deserialize the (un-paged) data
        else:
            response_content = json_dict

        # iterate through the returned data, deserializing each object found
        results_obj_list = []
        for object_dict in response_content:
            result_object = factory_class.of(object_dict)
            results_obj_list.append(result_object)

        return IcePagedResult(results_obj_list, count, next_page, prev_page)
