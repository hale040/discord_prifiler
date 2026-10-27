if (typeof HOME_URL === 'undefined'){
    var HOME_URL = '';
    for (i=0; i < document.getElementsByTagName('script').length; i++){
        let scripts = document.getElementsByTagName('script')[i];
        if(scripts.getAttribute('src') && scripts.getAttribute('src').indexOf('boxberry.js') > 0 &&  scripts.getAttribute('src').indexOf(window.location.hostname) == -1)
        {
            var src = document.getElementsByTagName('script')[i].getAttribute('src');
            var arrSrc = src.split('/');
            var HOME_URL = '';

            if (arrSrc.length  == 5){
				var protocol = (arrSrc[0] != "" && typeof arrSrc[0] != 'undefined') ? arrSrc[0] : 'https:';
				var address = (arrSrc[2] != "" && typeof arrSrc[2] != 'undefined')  ? arrSrc[2] : 'points.boxberry.ru';
				HOME_URL = protocol + '//' + address;
			} else if (arrSrc.length <= 3) {
            	if (src == '/js/boxberry.js'){
            		HOME_URL = ''
				} else {
                    HOME_URL = (arrSrc[0] != "" && typeof arrSrc[0] != 'undefined')  ? arrSrc[0] : 'https://points.boxberry.ru';
				}
            } else {
                HOME_URL = 'https://points.boxberry.ru';
			}

        }
    }
}
if (!Function.prototype.bind) {
		Function.prototype.bind = function(oThis) {
			if (typeof this !== 'function') {
				throw new TypeError('Function.prototype.bind - what is trying to be bound is not callable');
			}

			var aArgs = Array.prototype.slice.call(arguments, 1),
			fToBind = this,
			fNOP    = function() {},
			fBound  = function() {
				return fToBind.apply(this instanceof fNOP && oThis
					? this
					: oThis,
				aArgs.concat(Array.prototype.slice.call(arguments)));
			};

			fNOP.prototype = this.prototype;
			fBound.prototype = new fNOP();

			return fBound;
		};
	}
	function getCookie(name) {
	  var matches = document.cookie.match(new RegExp(
		"(?:^|; )" + name.replace(/([\.$?*|{}\(\)\[\]\\\/\+^])/g, '\\$1') + "=([^;]*)"
	  ));
	  return matches ? decodeURIComponent(matches[1]) : undefined;
	}
	var boxberry = {

		parameters : new Object(),
		displaySettings : new Object(),

		init: function(){
            this._callback_function = (this._callback_function ? this._callback_function : function(){return false;});
			this._overlay = (this._overlay ? this._overlay : null);
			this._frame = (this._frame ? this._frame : null);

			var h = document.getElementsByTagName('HEAD')[0];

			var el = document.createElement('LINK');
				el.rel = 'stylesheet';
				el.type = 'text/css';
				el.href = HOME_URL +'/css/boxberry.css';
				h.appendChild(el);
			var el = document.createElement('SCRIPT');
				el.src = HOME_URL + '/js/postmessage.js';
				el.onload = function(){
				pm.bind('boxberry-map-point-select', function(data){
						this.callCallbackSelfFunction(data);
						this.hideOverlay();
						this.hideContainer();
					}.bind(this));
				}.bind(this);
				h.appendChild(el);


		},
		versionAPI: function(val){
			if (val % 1 === 0){
				this.parameters.versionAPI = '';
			}else{
				this.parameters.versionAPI = encodeURIComponent(val);
			}
		},
		checkLocation: function(val){
            this.parameters.checkLocation = (val == 1 ? 1 : 0);
		},
		makeUrl: function(parameters){
			var url = '?';
			for(var index in parameters) {
			   url = url + index+'='+parameters[index]+'&';
			}
			return url+'host='+ location.hostname;
		},
        sucrh: function (toggle) {
            if (toggle==1){
                this.parameters.sucrh = 1;
            }else{
                this.parameters.sucrh = 0;
            }
        },
		openOnPage: function(element){
			this.parameters.element = element;
		},
		displaySettings: function (parameters){
			this.displaySettings.top = parameters.top;
		},
		open: function(callback_function, api_token, custom_city, target_start ,ordersum, weight, paysum, height, width, depth, prepaid ){
				this.parameters.calc = 0;
				if (prepaid	 % 1 === 0 ){
					this.parameters.prepaid = prepaid;
				}
				this.parameters.chgPaysum = (this.parameters.paysum == paysum ? false : true);
				this.parameters.chgCity = (this.parameters.custom_city == encodeURIComponent(custom_city) ? false : true);
				this.parameters.calc = 1;
				this.parameters.select_office = 1;
				this.parameters.kd=0;
				this.parameters.ordersum = (ordersum !== undefined ?  ordersum : '');
				this.parameters.paysum = (paysum !== undefined ?  paysum : '');
				this.parameters.weight = (weight !== undefined ?  weight : 0);
				this.parameters.height = (height !== undefined ?  height : 0);
				this.parameters.width = (width !== undefined ?  width : 0);
				this.parameters.depth = (depth !== undefined ?  depth : 0);

				if (this.parameters.paysum > 0){
					this.parameters.prepaid = 1;
				}
				if (api_token % 1 === 0){
						this.parameters.api_token = '';
						this.parameters.custom_city = encodeURIComponent(custom_city);
						this.parameters.target_start = '68';
					}else{
						this.parameters.api_token = (api_token !== undefined ? encodeURIComponent(api_token) : '');
						this.parameters.custom_city = (custom_city !== undefined ?  encodeURIComponent(custom_city) : '');
						if (target_start !=''){
							this.parameters.target_start = target_start;
						}
					}
				if (this.parameters.api_token % 1 === 0) {
					this.parameters.calc = 0;
				}

				if (typeof callback_function === 'string'){
					callback_function = window[callback_function];
				}
				if (callback_function == undefined){
					this.parameters.calc = 0;
					this.parameters.select_office = 0;
				}

				this._callback_function = callback_function;
				if (this.parameters.element){
						this.showContainerOnPage();
					}else{
						this.showOverlay();
						this.showContainer();
				}
		},
		openIherb: function(element, callback_function, api_token, custom_city, country_code){


			this.parameters.element = element;
			this.parameters.api_token = api_token;
			this.parameters.calc = 1;
			this.parameters.select_office = 1;
			this.parameters.iherb = 1;
			this.parameters.custom_city = (custom_city !== undefined ?  encodeURIComponent(custom_city) : '');
			this.parameters.country_code = (country_code !== undefined ?  country_code : 643 );

				if (typeof callback_function === 'string'){
					callback_function = window[callback_function];
				}

				this._callback_function = callback_function;
				if (this.parameters.element){
						this.showContainerOnPageIherb();
					}else{
						this.showOverlay();
						this.showContainer();
				}
		},
		openspvz: function(callback_function, api_token, custom_city, prepaid ){
				this.parameters.weight = 0;
				this.parameters.pip = 1;
				this.parameters.prepaid = 0;
				this.parameters.chgCity = (this.parameters.custom_city == encodeURIComponent(custom_city) ? false : true);
				this.parameters.calc = 0;
				if (prepaid	=== 1){
					this.parameters.prepaid = 1;
				}else{
                    this.parameters.prepaid = 0;
				}
				this.parameters.select_office = 1;


				this.parameters.kd=0;


					if (api_token % 1 === 0){
						this.parameters.api_token = '';
						this.parameters.custom_city = encodeURIComponent(custom_city);
						this.parameters.target_start = '68';
					}else{
						this.parameters.api_token = (api_token !== undefined ? encodeURIComponent(api_token) : '');
						this.parameters.custom_city = (custom_city !== undefined ?  encodeURIComponent(custom_city) : '');

					}

				if (typeof callback_function === 'string'){
					callback_function = window[callback_function];
				}
				if (callback_function == undefined){
					this.parameters.calc = 0;
					this.parameters.select_office = 0;
				}

				this._callback_function = callback_function;
				this.showOverlay();
				this.showContainer();
		},
		openKD: function(callback_function, api_token, custom_city, uniqNameCity, regionName){
			this._callback_function = callback_function;

			this.parameters.kd=1;
			this.parameters.calc = 0;
			this.parameters.select_office = 0;

			this.parameters.api_token = (api_token !== undefined ? encodeURIComponent(api_token) : '');
			this.parameters.custom_city = (custom_city !== undefined ?  encodeURIComponent(custom_city) : '');
			this.parameters.uniqnamecity = (uniqNameCity !== undefined ?  encodeURIComponent(uniqNameCity) : '');
			this.parameters.regionName = (regionName !== undefined ?  encodeURIComponent(regionName) : '');
			this.showOverlay();
			this.showContainer();
		},
		callCallbackSelfFunction: function(){
			if (typeof (this._callback_function) != 'undefined' ){
				this._callback_function.apply(window, arguments);
			}
		},

		showOverlay: function(){
			if (!this._overlay){
				this._overlay = document.createElement('DIV');
				this._overlay.className = 'boxberry_overlay';
				document.getElementsByTagName('BODY')[0].appendChild(this._overlay);
			}
			this._overlay.style.display = 'block';
		},

		hideOverlay: function(){

			if (this._overlay){
				this._overlay.style.display = 'none';
			}
		},
		showContainerOnPage: function(){
			content = document.getElementById(this.parameters.element);
			if (content){
				this._frame = document.createElement('IFRAME');
				this._frame.src =   HOME_URL + '/map/' + this.makeUrl (this.parameters);
				this._frame.frameBorder = "0";
				this._frame.height = "560px";
				this._frame.width = "100%";
				content.appendChild(this._frame);
			}
		},
		showContainerOnPageIherb: function(){
			content = document.getElementById(this.parameters.element);
			if (content){
				this._frame = document.createElement('IFRAME');
				this._frame.src =   HOME_URL + '/map/' + this.makeUrl (this.parameters);
				this._frame.frameBorder = "0";
				this._frame.height = "820px";
				this._frame.width = "100%";
				content.appendChild(this._frame);
			}
		},
		showContainer: function(){

			//if (this.parameters.reloadmap){
				this._container = document.createElement('DIV');
				this._container.className = 'boxberry_container';

				document.getElementsByTagName('BODY')[0].appendChild(this._container);

				var toppanel = document.createElement('DIV');
					toppanel.className = 'boxberry_toppanel';
					var a = document.createElement('A');
					a.href = '#';
					a.innerHTML = '<img src="' + HOME_URL + '/img/close_round_button.png" alt="close">';
					a.className = 'boxberry_container_close';
					a.onclick = function(){
						this.hideOverlay();
						this.hideContainer();
						return false;
					}.bind(this);
				toppanel.appendChild(a);
				this._container.appendChild(toppanel);
				this._container.style.display = 'block';
				var s = this.getPageScroll();

				ytop = document.documentElement.clientHeight;
				this._container.style.top = '30px';

				if (this.displaySettings.top != undefined){
					this._container.style.top = this.displaySettings.top + 'px';
				}
				if (window.innerWidth <= 600){
					//this._container.style.top = (s.top + Math.max(0, (ytop - this._container.offsetHeight) / 2)) + 'px';
					if (s.top > 0){
						this._container.style.top = (s.top + 30) + 'px';
					}
				}
				this._container.style.left = (s.left + Math.max(0, (document.documentElement.clientWidth - this._container.offsetWidth) / 2)) + 'px';

				var content = document.createElement('DIV');
					content.className = 'boxberry_content';
				this._content = content;
				this._container.appendChild(content);

					this._frame = document.createElement('IFRAME');
					this._frame.src =   HOME_URL + '/map/' + this.makeUrl (this.parameters);

					this._frame.frameborder = '0';
					this._frame.style.border = '0';

					this._frame.height = '560px';
					this._frame.data = document.documentElement.clientHeight;
					this._frame.style.width = '100%';
					this._frame.id = 'boxberry_map';
					content.appendChild(this._frame);

				if (this._frame.contentWindow){
					pm({
						target: this._frame.contentWindow,
						type: 'boxberry-map-init',
						data: {init:true}
					});
				}
			//}
		},

		hideContainer: function(){
			if (this._frame){
				pm({
					target: this._frame.contentWindow,
					type: 'boxberry-map-destroy',
					data: {}
				});
			}
			if (this._container){
				if(typeof this._container.remove=='function'){
					this._container.remove()
				}else{
					this._container.outerHTML='';
				}
				this._container.style.display = 'none';
			}

		},

		getPageScroll: function(){

			var doc = document;
			var html = doc.documentElement;
			var body = doc.body;

			var top = (window && window.pageYOffset) || (doc && doc.scrollTop) || (html && html.scrollTop) || 0;
				top -= html.clientTop;
			var left = (doc && doc.scrollLeft) || (doc.body && doc.body.scrollLeft) || (html && html.scrollLeft) || 0;
				left -= html.clientLeft;

			return {
				top: top,
				left: left
			};
		}
	};

	boxberry.init();

	function setBXBCookie(key, value) {
		var expires = new Date();
		expires.setTime(expires.getTime() + (30 * 60 * 1000));
		document.cookie = key + '=' + value + ';expires=' + expires.toUTCString();
	}

	function pvz_delivery_remote(result){

		var settings_element = getCookie('element_bxb');
			if (settings_element != null){
				var element = document.getElementById(settings_element);
			}

			if (element != undefined){
				setBXBCookie('bxb_city', result.name);
				setBXBCookie('bxb_address', result.address);
				element.value= 'Boxberry: #'+result.id+'# '+ result.address + "\n";
				setBXBCookie('bxb_price', result.price);
				setBXBCookie('bxb_period', result.period);
				if (settings_element.indexOf("soa") == -1){
					submitForm();
				}
			}

			return false;
	}
	function pvz_delivery_cod_remote(result){
		var settings_element = getCookie('element_bxb');
			if (settings_element != null){
				var element = document.getElementById(settings_element);
			}

			if (element != undefined){

				setBXBCookie('bxb_city', result.name);
				setBXBCookie('bxb_address', result.address);
				element.value= 'Boxberry: #'+result.id+'# '+ result.address + "\n";
				setBXBCookie('bxb_price_cod', result.price);
				setBXBCookie('bxb_period_cod', result.period);
				if (settings_element.indexOf("soa") == -1){
					submitForm();
				}
			}
		return false;
	}

	function delivery_remote(result){
		var settings_element = getCookie('element_bxb');
			if (settings_element != null){
				var element = document.getElementById(settings_element);
			}

			if (element != undefined){
				setBXBCookie('bxb_id', result.id);
				element.value = 'Boxberry: #'+result.id+'# '+ result.address + "\n";
				setBXBCookie('bxb_price', result.price);
				setBXBCookie('bxb_period', result.period);
			}
			return false;
	}
	function delivery_cod_remote(result){
		var settings_element = getCookie('element_bxb');
			if (settings_element != null){
				var element = document.getElementById(settings_element);
			}
			if (element != undefined){
				setBXBCookie('bxb_id', result.id);
				element.value = 'Boxberry: #'+result.id+'# '+ result.address + "\n";
				setBXBCookie('bxb_price_cod', result.price);
				setBXBCookie('bxb_period_cod', result.period);

			}
		return false;
	}

	setBXBCookie('settings_activate', 1);
