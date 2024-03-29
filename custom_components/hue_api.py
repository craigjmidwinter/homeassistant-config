"""Provides a Hue API to control Home Assistant."""
import asyncio
import logging

from aiohttp import web

from homeassistant import core
from homeassistant.const import (
    ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON, SERVICE_VOLUME_SET,
    SERVICE_OPEN_COVER, SERVICE_CLOSE_COVER, STATE_ON, STATE_OFF,
    HTTP_BAD_REQUEST, HTTP_NOT_FOUND, ATTR_SUPPORTED_FEATURES,
)
import homeassistant.util.color as color_util
from homeassistant.components.light import (
    ATTR_BRIGHTNESS, ATTR_COLOR_TEMP, ATTR_RGB_COLOR, ATTR_XY_COLOR,
    SUPPORT_BRIGHTNESS, SUPPORT_COLOR_TEMP, SUPPORT_RGB_COLOR, SUPPORT_XY_COLOR
)
from homeassistant.components.media_player import (
    ATTR_MEDIA_VOLUME_LEVEL, SUPPORT_VOLUME_SET,
)
from homeassistant.components.fan import (
    ATTR_SPEED, SUPPORT_SET_SPEED, SPEED_OFF, SPEED_LOW,
    SPEED_MEDIUM, SPEED_HIGH
)
from homeassistant.components.http import HomeAssistantView

_LOGGER = logging.getLogger(__name__)

ATTR_EMULATED_HUE = 'emulated_hue'
ATTR_EMULATED_HUE_NAME = 'emulated_hue_name'

HUE_API_DEVICE_TYPE_DIMMABLE = 'Dimmable light'
HUE_API_DEVICE_TYPE_COLOR_TEMP = 'Color temperature light'
HUE_API_DEVICE_TYPE_COLOR = 'Color light'
HUE_API_DEVICE_TYPE_EXTENDED_COLOR = 'Extended color light'

HUE_API_STATE_ON = 'on'
HUE_API_STATE_BRI = 'bri'
HUE_API_STATE_COLORMODE = 'colormode'
HUE_API_STATE_CT = 'ct'
HUE_API_STATE_XY = 'xy'


class HueUsernameView(HomeAssistantView):
    """Handle requests to create a username for the emulated hue bridge."""

    url = '/api'
    name = 'emulated_hue:api:create_username'
    extra_urls = ['/api/']
    requires_auth = False

    @asyncio.coroutine
    def post(self, request):
        """Handle a POST request."""
        try:
            data = yield from request.json()
        except ValueError:
            return self.json_message('Invalid JSON', HTTP_BAD_REQUEST)

        if 'devicetype' not in data:
            return self.json_message('devicetype not specified',
                                     HTTP_BAD_REQUEST)

        return self.json([{'success': {'username': '12345678901234567890'}}])


class HueAllLightsStateView(HomeAssistantView):
    """Handle requests for getting and setting info about entities."""

    url = '/api/{username}/lights'
    name = 'emulated_hue:lights:state'
    requires_auth = False

    def __init__(self, config):
        """Initialize the instance of the view."""
        self.config = config

    @core.callback
    def get(self, request, username):
        """Process a request to get the list of available lights."""
        hass = request.app['hass']
        json_response = {}

        for entity in hass.states.async_all():
            if self.config.is_entity_exposed(entity):
                state, device_type = get_entity_state(self.config, entity)

                number = self.config.entity_id_to_number(entity.entity_id)
                json_response[number] = entity_to_json(
                    entity, state, device_type)

        return self.json(json_response)


class HueOneLightStateView(HomeAssistantView):
    """Handle requests for getting and setting info about entities."""

    url = '/api/{username}/lights/{entity_id}'
    name = 'emulated_hue:light:state'
    requires_auth = False

    def __init__(self, config):
        """Initialize the instance of the view."""
        self.config = config

    @core.callback
    def get(self, request, username, entity_id):
        """Process a request to get the state of an individual light."""
        hass = request.app['hass']
        entity_id = self.config.number_to_entity_id(entity_id)
        entity = hass.states.get(entity_id)

        if entity is None:
            _LOGGER.error('Entity not found: %s', entity_id)
            return web.Response(text="Entity not found", status=404)

        if not self.config.is_entity_exposed(entity):
            _LOGGER.error('Entity not exposed: %s', entity_id)
            return web.Response(text="Entity not exposed", status=404)

        state, device_type = get_entity_state(self.config, entity)

        json_response = entity_to_json(entity, state, device_type)

        return self.json(json_response)


class HueOneLightChangeView(HomeAssistantView):
    """Handle requests for getting and setting info about entities."""

    url = '/api/{username}/lights/{entity_number}/state'
    name = 'emulated_hue:light:state'
    requires_auth = False

    def __init__(self, config):
        """Initialize the instance of the view."""
        self.config = config

    @asyncio.coroutine
    def put(self, request, username, entity_number):
        """Process a request to set the state of an individual light."""
        config = self.config
        hass = request.app['hass']
        entity_id = config.number_to_entity_id(entity_number)

        if entity_id is None:
            _LOGGER.error('Unknown entity number: %s', entity_number)
            return self.json_message('Entity not found', HTTP_NOT_FOUND)

        entity = hass.states.get(entity_id)

        if entity is None:
            _LOGGER.error('Entity not found: %s', entity_id)
            return self.json_message('Entity not found', HTTP_NOT_FOUND)

        if not config.is_entity_exposed(entity):
            _LOGGER.error('Entity not exposed: %s', entity_id)
            return web.Response(text="Entity not exposed", status=404)

        try:
            request_json = yield from request.json()
        except ValueError:
            _LOGGER.error('Received invalid json')
            return self.json_message('Invalid JSON', HTTP_BAD_REQUEST)

        # Parse the request into requested "on" status and brightness
        parsed = parse_hue_api_put_light_body(request_json, entity)

        if parsed is None:
            _LOGGER.error('Unable to parse data: %s', request_json)
            return web.Response(text="Bad request", status=400)

        result, brightness, color, color_temp = parsed

        # Choose general HA domain
        domain = core.DOMAIN

        # Entity needs separate call to turn on
        turn_on_needed = False

        # Convert the resulting "on" status into the service we need to call
        service = SERVICE_TURN_ON if result else SERVICE_TURN_OFF

        # Construct what we need to send to the service
        data = {ATTR_ENTITY_ID: entity_id}

        # Make sure the entity actually supports brightness
        entity_features = entity.attributes.get(ATTR_SUPPORTED_FEATURES, 0)

        if entity.domain == "light":
            if entity_features & SUPPORT_BRIGHTNESS:
                if brightness is not None:
                    data[ATTR_BRIGHTNESS] = brightness
            if entity_features & SUPPORT_XY_COLOR:
                if color is not None:
                    data[ATTR_XY_COLOR] = color
            elif entity_features & SUPPORT_RGB_COLOR:
                if color is not None:
                    if brightness is not None:
                        final_brightness = brightness
                    else:
                        final_brightness = entity.attributes.get(
                            ATTR_BRIGHTNESS, 255 if result else 0)
                    data[ATTR_RGB_COLOR] = \
                        color_util.color_xy_brightness_to_RGB(color[0],
                                                              color[1],
                                                              final_brightness)
            if entity_features & SUPPORT_COLOR_TEMP:
                if color_temp is not None:
                    data[ATTR_COLOR_TEMP] = color_temp

        # If the requested entity is a script add some variables
        elif entity.domain == "script":
            data['variables'] = {
                'requested_state': STATE_ON if result else STATE_OFF
            }

            if brightness is not None:
                data['variables']['requested_level'] = brightness

        # If the requested entity is a media player, convert to volume
        elif entity.domain == "media_player":
            if entity_features & SUPPORT_VOLUME_SET:
                if brightness is not None:
                    turn_on_needed = True
                    domain = entity.domain
                    service = SERVICE_VOLUME_SET
                    # Convert 0-100 to 0.0-1.0
                    data[ATTR_MEDIA_VOLUME_LEVEL] = brightness / 100.0

        # If the requested entity is a cover, convert to open_cover/close_cover
        elif entity.domain == "cover":
            domain = entity.domain
            if service == SERVICE_TURN_ON:
                service = SERVICE_OPEN_COVER
            else:
                service = SERVICE_CLOSE_COVER

        # If the requested entity is a fan, convert to speed
        elif entity.domain == "fan":
            if entity_features & SUPPORT_SET_SPEED:
                if brightness is not None:
                    domain = entity.domain
                    # Convert 0-100 to a fan speed
                    if brightness == 0:
                        data[ATTR_SPEED] = SPEED_OFF
                    elif brightness <= 33.3 and brightness > 0:
                        data[ATTR_SPEED] = SPEED_LOW
                    elif brightness <= 66.6 and brightness > 33.3:
                        data[ATTR_SPEED] = SPEED_MEDIUM
                    elif brightness <= 100 and brightness > 66.6:
                        data[ATTR_SPEED] = SPEED_HIGH

        if entity.domain in config.off_maps_to_on_domains:
            # Map the off command to on
            service = SERVICE_TURN_ON

            # Caching is required because things like scripts and scenes won't
            # report as "off" to Alexa if an "off" command is received, because
            # they'll map to "on". Thus, instead of reporting its actual
            # status, we report what Alexa will want to see, which is the same
            # as the actual requested command.
            config.cached_states[entity_id] = (result, brightness)

        # Separate call to turn on needed
        if turn_on_needed:
            hass.async_add_job(hass.services.async_call(
                core.DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: entity_id},
                blocking=True))

        hass.async_add_job(hass.services.async_call(
            domain, service, data, blocking=True))

        json_response = \
            [create_hue_success_response(entity_id, HUE_API_STATE_ON, result)]

        if brightness is not None:
            json_response.append(create_hue_success_response(
                entity_id, HUE_API_STATE_BRI, brightness))

        if color is not None:
            json_response.append(create_hue_success_response(
                entity_id, HUE_API_STATE_XY, color))

        if color_temp is not None:
            json_response.append(create_hue_success_response(
                entity_id, HUE_API_STATE_CT, color_temp))

        return self.json(json_response)


def parse_hue_api_put_light_body(request_json, entity):
    """Parse the body of a request to change the state of a light."""
    result = None
    brightness = None
    color = None
    color_temp = None

    if HUE_API_STATE_ON in request_json:
        if not isinstance(request_json[HUE_API_STATE_ON], bool):
            return None

        result = request_json['on']

    # Make sure the entity actually supports color temperature
    entity_features = entity.attributes.get(ATTR_SUPPORTED_FEATURES, 0)

    if HUE_API_STATE_BRI in request_json:
        try:
            # Clamp brightness from 0 to 255
            hue_brightness = \
                max(0, min(int(request_json[HUE_API_STATE_BRI]), 255))
        except ValueError:
            return None

        if entity.domain == "light":
            if entity_features & SUPPORT_BRIGHTNESS:
                brightness = hue_brightness
                result = (brightness > 0)

        elif (entity.domain == "script" or
              entity.domain == "media_player" or
              entity.domain == "fan"):
            # Convert 0-255 to 0-100
            level = hue_brightness / 255 * 100
            brightness = round(level)
            result = True

    if HUE_API_STATE_XY in request_json:
        if not isinstance(request_json[HUE_API_STATE_XY], list) or \
                len(request_json[HUE_API_STATE_XY]) != 2:
            return None

        if entity.domain == "light":
            if entity_features & SUPPORT_XY_COLOR or \
                    entity_features & SUPPORT_RGB_COLOR:
                color = tuple(request_json[HUE_API_STATE_XY])

    if HUE_API_STATE_CT in request_json:
        try:
            # Clamp to supported range
            hue_ct = max(color_util.HASS_COLOR_MIN, min(
                int(request_json[HUE_API_STATE_CT]),
                color_util.HASS_COLOR_MAX))
        except ValueError:
            return None

        if entity.domain == "light":
            if entity_features & SUPPORT_COLOR_TEMP:
                color_temp = hue_ct

    return (result, brightness, color, color_temp)


def get_entity_state(config, entity):
    """Retrieve and convert state and brightness values for an entity."""
    cached_state = config.cached_states.get(entity.entity_id, None)
    device_type = HUE_API_DEVICE_TYPE_DIMMABLE

    if cached_state is None:
        is_on = entity.state != STATE_OFF

        state = {HUE_API_STATE_ON: is_on}
        brightness = entity.attributes.get(ATTR_BRIGHTNESS,
                                           255 if is_on else 0)
        state[HUE_API_STATE_BRI] = brightness

        if entity.domain == "light":
            # Make sure the entity actually supports brightness
            entity_features = entity.attributes.get(ATTR_SUPPORTED_FEATURES, 0)

            if entity_features & SUPPORT_COLOR_TEMP:
                color_temp = entity.attributes.get(ATTR_COLOR_TEMP, 0)
                state[HUE_API_STATE_CT] = color_temp

            if entity_features & SUPPORT_XY_COLOR:
                xy_color = entity.attributes.get(ATTR_XY_COLOR, [0.0, 0.0])
                state[HUE_API_STATE_XY] = xy_color
            elif entity_features & SUPPORT_RGB_COLOR:
                rgb_color = entity.attributes.get(ATTR_RGB_COLOR, [0, 0, 0])
                xy_color = color_util.color_RGB_to_xy(
                    *(int(val) for val in rgb_color))
                state[HUE_API_STATE_XY] = (xy_color[0], xy_color[1])

            if entity_features & SUPPORT_XY_COLOR or \
                    entity_features & SUPPORT_RGB_COLOR:
                state[HUE_API_STATE_COLORMODE] = HUE_API_STATE_XY
                if entity_features & SUPPORT_COLOR_TEMP:
                    device_type = HUE_API_DEVICE_TYPE_EXTENDED_COLOR
                else:
                    device_type = HUE_API_DEVICE_TYPE_COLOR
            elif entity_features & SUPPORT_COLOR_TEMP:
                state[HUE_API_STATE_COLORMODE] = HUE_API_STATE_CT
                device_type = HUE_API_DEVICE_TYPE_COLOR_TEMP

        elif entity.domain == "media_player":
            level = entity.attributes.get(
                ATTR_MEDIA_VOLUME_LEVEL, 1.0 if is_on else 0.0)
            # Convert 0.0-1.0 to 0-255
            state[HUE_API_STATE_BRI] = round(min(1.0, level) * 255)

        elif entity.domain == "fan":
            speed = entity.attributes.get(ATTR_SPEED, 0)
            # Convert 0.0-1.0 to 0-255
            state[HUE_API_STATE_BRI] = 0
            if speed == SPEED_LOW:
                state[HUE_API_STATE_BRI] = 85
            elif speed == SPEED_MEDIUM:
                state[HUE_API_STATE_BRI] = 170
            elif speed == SPEED_HIGH:
                state[HUE_API_STATE_BRI] = 255

    else:
        final_state, final_brightness = cached_state

        state = {HUE_API_STATE_ON: final_state}
        # Make sure brightness is valid
        if final_brightness is None:
            state[HUE_API_STATE_BRI] = 255 if final_state else 0

    return (state, device_type)


def entity_to_json(entity, state, device_type):
    """Convert an entity to its Hue bridge JSON representation."""
    name = entity.attributes.get(ATTR_EMULATED_HUE_NAME, entity.name)

    state['reachable'] = True

    return {
        'state': state,
        'type': device_type,
        'name': name,
        'modelid': 'HASS123',
        'uniqueid': entity.entity_id,
        'swversion': '123'
    }


def create_hue_success_response(entity_id, attr, value):
    """Create a success response for an attribute set on a light."""
    success_key = '/lights/{}/state/{}'.format(entity_id, attr)
    return {'success': {success_key: value}}